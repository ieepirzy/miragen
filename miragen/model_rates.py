"""Per-model token rates, read from a configured source, for budget weighting.

MiraGen keeps no rates of its own. A run's budget (models.budget_tokens)
weights cache reads and cache writes by the ratio of the model's own rates
to its fresh-input rate, and those rates come only from a source the
operator configures:

- ``MIRAGEN_MODEL_RATES_URL`` (+ optional ``MIRAGEN_MODEL_RATES_TOKEN``, sent
  as a bearer token): an HTTP endpoint, typically a control plane's model
  catalogue. Fetched at startup and every ``MIRAGEN_MODEL_RATES_REFRESH_S``
  seconds (default 900). The last good copy is kept on disk, so a restart
  without network still has rates.
- ``MIRAGEN_MODEL_RATES_PATH``: a local JSON file, for self-hosters. Re-read
  when it changes. (Not ``*_FILE``: that suffix is the secret-file
  convention, and the startup loader would consume the variable.)

Both serve the same document, rates per million tokens with decimals as
strings::

    {"models": {"<model string>": {"input": "<rate>", "cached_input": "<rate>",
                                   "cache_write": "<rate>", "cache_write_1h": "<rate>",
                                   "output": "<rate>"}}}

`cache_write` (5-minute TTL) and `cache_write_1h` are optional. Only the
ratios matter, so the currency is irrelevant. Reading is tolerant: unknown
fields are ignored and a malformed entry is skipped with a log line.

With NO source configured, budgets count exactly as before rates existed:
cache reads are left out and cache writes count 1:1 (models.budget_tokens).
Configuring a source never loosens that for a model it can't weight: for a
model the source doesn't list, or one whose input rate is 0 (a flat-rate
subscription harness), cache reads and writes count in full, one budget
token per token. Each such model is logged once.

Other sources plug in through the `RatesSource` protocol.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol

import httpx

logger = logging.getLogger(__name__)

URL_ENV = "MIRAGEN_MODEL_RATES_URL"
TOKEN_ENV = "MIRAGEN_MODEL_RATES_TOKEN"
PATH_ENV = "MIRAGEN_MODEL_RATES_PATH"
REFRESH_ENV = "MIRAGEN_MODEL_RATES_REFRESH_S"
DEFAULT_REFRESH_S = 900.0
MIN_REFRESH_S = 30.0
FETCH_TIMEOUT_S = 10.0  # wall clock for one whole fetch
STARTUP_FETCH_TIMEOUT_S = 5.0  # the synchronous first fetch with no copy on disk
# A rates document is a few KB; anything far larger is not one.
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
CACHE_FILE_NAME = "rates.json"

RatesTable = Mapping[str, "ModelRates"]


class RatesDocumentInvalid(ValueError):
    """The document as a whole isn't a rates document (no `models` object)."""


@dataclass(frozen=True, slots=True)
class ModelRates:
    """One model's rates per million tokens, exact."""

    input: Fraction
    cached_input: Fraction
    output: Fraction
    cache_write: Optional[Fraction] = None
    cache_write_1h: Optional[Fraction] = None


@dataclass(frozen=True, slots=True)
class BudgetWeights:
    """Budget tokens per token, relative to one fresh input token."""

    cache_read: Fraction
    cache_write: Fraction
    cache_write_1h: Fraction


# Every token counts once: the fallback whenever rates can't weight.
FULL_WEIGHTS = BudgetWeights(Fraction(1), Fraction(1), Fraction(1))


def weights_for(rates: Optional[ModelRates]) -> Optional[BudgetWeights]:
    """Weights from a model's rates, or None when they can't weight (no
    rates, or a zero input rate, where any ratio is meaningless).

    Reads weigh `cached_input / input`. 5-minute writes weigh
    `cache_write / input`, 1 when the rate is absent (a write billed as
    ordinary input). 1-hour writes weigh `cache_write_1h / input`; when that
    rate is absent, the dearest known write weight (the 5-minute weight or
    1, whichever is higher)."""
    if rates is None or rates.input <= 0:
        return None
    write = rates.cache_write / rates.input if rates.cache_write is not None else Fraction(1)
    write_1h = (
        rates.cache_write_1h / rates.input
        if rates.cache_write_1h is not None
        else max(write, Fraction(1))
    )
    return BudgetWeights(rates.cached_input / rates.input, write, write_1h)


# ── parsing ─────────────────────────────────────────────────────────────────

_REQUIRED = ("input", "cached_input", "output")
_OPTIONAL = ("cache_write", "cache_write_1h")


def _rate(value: Any, field: str) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"{field} must be a decimal string")
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation as error:
        raise ValueError(f"{field} must be a decimal string") from error
    if not number.is_finite() or number < 0:
        raise ValueError(f"{field} must be a finite, non-negative decimal")
    return Fraction(number)


def _entry(raw: Any) -> ModelRates:
    if not isinstance(raw, dict):
        raise ValueError("entry is not an object")
    values: dict[str, Optional[Fraction]] = {}
    for field in _REQUIRED:
        if raw.get(field) is None:
            raise ValueError(f"{field} is missing")
        values[field] = _rate(raw[field], field)
    for field in _OPTIONAL:
        values[field] = None if raw.get(field) is None else _rate(raw[field], field)
    return ModelRates(**values)  # type: ignore[arg-type]


def parse_rates_document(document: Any) -> dict[str, ModelRates]:
    """Rates by model string. Raises RatesDocumentInvalid only when the
    document has no `models` object; a bad entry is skipped and logged, and
    unknown fields anywhere are ignored."""
    if not isinstance(document, dict) or not isinstance(document.get("models"), dict):
        raise RatesDocumentInvalid("a rates document is an object with a `models` object")
    table: dict[str, ModelRates] = {}
    for model, raw in document["models"].items():
        if not isinstance(model, str) or not model.strip():
            logger.warning("model rates: skipping an entry with an empty model string")
            continue
        try:
            table[model.strip()] = _entry(raw)
        except ValueError as error:
            logger.warning("model rates: skipping %r: %s", model, error)
    return table


# ── sources ─────────────────────────────────────────────────────────────────


class RatesSource(Protocol):
    """Where model rates come from. `current()` never blocks on the network:
    it returns the last good table, or None when there has never been one."""

    def current(self) -> Optional[RatesTable]: ...


class StaticRatesSource:
    """A fixed table (tests, embedding)."""

    def __init__(self, table: RatesTable) -> None:
        self._table = dict(table)

    def current(self) -> Optional[RatesTable]:
        return self._table


class FileRatesSource:
    """A local JSON rates document, re-read whenever its mtime changes. An
    unreadable or invalid file keeps the last good table."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._mtime: Optional[float] = None
        self._table: Optional[dict[str, ModelRates]] = None
        self._lock = threading.Lock()

    def current(self) -> Optional[RatesTable]:
        with self._lock:
            try:
                mtime = self._path.stat().st_mtime
            except OSError:
                if self._table is None:
                    _warn_once(f"file:{self._path}",
                               "model rates file %s is not readable", self._path)
                return self._table
            if mtime != self._mtime:
                self._mtime = mtime
                try:
                    if self._path.stat().st_size > MAX_DOCUMENT_BYTES:
                        raise RatesDocumentInvalid("file is too large for a rates document")
                    self._table = parse_rates_document(json.loads(self._path.read_text("utf-8")))
                except (OSError, ValueError) as error:
                    logger.warning("model rates file %s not loaded (%s); keeping the last "
                                   "good rates", self._path, error)
            return self._table


class HttpRatesSource:
    """Rates fetched from an HTTP endpoint on a background thread, refreshed
    every `refresh_s`, the last good copy kept on disk at `cache_path`.

    A failed fetch (network, status, size, JSON, schema) keeps the last good
    table. Every fetch is bounded by `timeout_s`."""

    def __init__(
        self,
        url: str,
        *,
        token: Optional[str] = None,
        cache_path: Optional[Path] = None,
        refresh_s: float = DEFAULT_REFRESH_S,
        timeout_s: float = FETCH_TIMEOUT_S,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.url = url
        self._token = token
        self._cache_path = Path(cache_path) if cache_path is not None else None
        self.refresh_s = max(float(refresh_s), MIN_REFRESH_S)
        self._timeout_s = timeout_s
        self._transport = transport
        self._lock = threading.Lock()
        self._table: Optional[dict[str, ModelRates]] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._failing = False
        self._load_cache()

    def current(self) -> Optional[RatesTable]:
        with self._lock:
            return self._table

    def refresh(self, timeout_s: Optional[float] = None) -> bool:
        """Fetch once, within `timeout_s` of wall clock (default: the
        source's fetch timeout). True when a new table was stored."""
        try:
            table = self._fetch(self._timeout_s if timeout_s is None else timeout_s)
        except (httpx.HTTPError, OSError, ValueError) as error:
            if not self._failing:
                logger.warning("model rates fetch from %s failed (%s); keeping the last "
                               "good rates", self.url, error)
            self._failing = True
            return False
        if self._failing:
            logger.info("model rates fetch from %s recovered", self.url)
        self._failing = False
        with self._lock:
            self._table = table
        self._write_cache(table)
        return True

    def start(self, startup_timeout_s: float = STARTUP_FETCH_TIMEOUT_S) -> None:
        """Start refreshing in the background. With no last good copy on
        disk, first fetch once synchronously (bounded by
        `startup_timeout_s`), so the first budget check already has rates
        when the source is reachable."""
        if self._thread is not None:
            return
        initial = self.current() is None
        if initial:
            self.refresh(startup_timeout_s)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="miragen-model-rates", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=self._timeout_s + 1)

    def _run(self) -> None:
        # start() has just fetched (or loaded the copy on disk): wait first.
        while not self._stop.wait(self.refresh_s):
            self.refresh()

    def _fetch(self, timeout_s: float) -> dict[str, ModelRates]:
        """One GET within `timeout_s` of wall clock in total. httpx bounds
        each phase (connect, each read) on its own; the deadline is checked
        between body chunks too, so a server trickling bytes can't hold a
        fetch open past it."""
        deadline = time.monotonic() + timeout_s
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        with httpx.Client(timeout=timeout_s, transport=self._transport) as client:
            with client.stream("GET", self.url, headers=headers) as response:
                if response.status_code != 200:
                    raise ValueError(f"HTTP {response.status_code}")
                body = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() > deadline:
                        raise ValueError(f"fetch exceeded {timeout_s:g}s")
                    body.extend(chunk)
                    if len(body) > MAX_DOCUMENT_BYTES:
                        raise ValueError("response is too large for a rates document")
        if time.monotonic() > deadline:
            raise ValueError(f"fetch exceeded {timeout_s:g}s")
        return parse_rates_document(json.loads(bytes(body)))

    def _load_cache(self) -> None:
        if self._cache_path is None or not self._cache_path.exists():
            return
        try:
            self._table = parse_rates_document(json.loads(self._cache_path.read_text("utf-8")))
            logger.info("model rates: loaded %d model(s) from the last good copy at %s",
                        len(self._table), self._cache_path)
        except (OSError, ValueError) as error:
            logger.warning("model rates: last good copy at %s not loaded (%s)",
                           self._cache_path, error)

    def _write_cache(self, table: Mapping[str, ModelRates]) -> None:
        if self._cache_path is None:
            return
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_name(self._cache_path.name + ".tmp")
            tmp.write_text(json.dumps(rates_document(table)), "utf-8")
            os.replace(tmp, self._cache_path)
        except OSError as error:
            logger.warning("model rates: could not keep a copy at %s (%s)",
                           self._cache_path, error)


def _decimal_text(value: Fraction) -> str:
    # Rates arrive as finite decimals, so the division is exact.
    return format(Decimal(value.numerator) / Decimal(value.denominator), "f")


def rates_document(table: Mapping[str, ModelRates]) -> dict[str, Any]:
    """A table back in the document shape (the on-disk copy)."""
    models: dict[str, Any] = {}
    for model, rates in table.items():
        entry = {
            "input": _decimal_text(rates.input),
            "cached_input": _decimal_text(rates.cached_input),
            "output": _decimal_text(rates.output),
        }
        for field in _OPTIONAL:
            value = getattr(rates, field)
            if value is not None:
                entry[field] = _decimal_text(value)
        models[model] = entry
    return {"models": models}


def source_from_env(
    environ: Mapping[str, str], state_dir: Optional[Path] = None
) -> Optional[RatesSource]:
    """The source the environment configures, or None. An HTTP source keeps
    its last good copy under `state_dir`."""
    url = (environ.get(URL_ENV) or "").strip()
    path = (environ.get(PATH_ENV) or "").strip()
    if url:
        if path:
            logger.warning("%s and %s are both set; using %s", URL_ENV, PATH_ENV, URL_ENV)
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"{URL_ENV} must be an http(s) URL")
        raw_refresh = (environ.get(REFRESH_ENV) or "").strip()
        try:
            refresh = float(raw_refresh) if raw_refresh else DEFAULT_REFRESH_S
        except ValueError as error:
            raise ValueError(f"{REFRESH_ENV} must be a number of seconds") from error
        return HttpRatesSource(
            url,
            token=(environ.get(TOKEN_ENV) or "").strip() or None,
            cache_path=(state_dir / CACHE_FILE_NAME) if state_dir is not None else None,
            refresh_s=refresh,
        )
    if path:
        return FileRatesSource(Path(path))
    return None


# ── the process's source ────────────────────────────────────────────────────

_source: Optional[RatesSource] = None


def configure(source: Optional[RatesSource]) -> None:
    """Set (or clear) the source budgets read from."""
    global _source
    _source = source


def configured_source() -> Optional[RatesSource]:
    return _source


def current_table() -> Optional[RatesTable]:
    """The configured source's table; None only when NO source is configured
    (budgets then count as before rates existed). A configured source that
    has no rates yet, or fails, gives an empty table: every model is then
    unlisted and its cache tokens count in full."""
    if _source is None:
        return None
    try:
        table = _source.current()
    except Exception as error:  # noqa: BLE001 — a broken source must not break a run
        _warn_once("source-error", "model rates source failed (%s); cache reads count "
                   "in full", error)
        return {}
    if table is None:
        _warn_once("source-empty", "the model rates source has no rates yet; cache reads "
                   "count in full until it does")
        return {}
    return table


# ── matching a model to its rates ───────────────────────────────────────────

_warned: set[str] = set()
_warned_lock = threading.Lock()


def _warn_once(key: str, message: str, *args: Any) -> None:
    with _warned_lock:
        if key in _warned:
            return
        _warned.add(key)
    logger.warning(message, *args)


# A provider snapshot date on a reported id: `claude-x-20260901`, `gpt-x-2026-09-01`.
_SNAPSHOT_SUFFIX = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$")


def _bare(model: str) -> str:
    """The id without a `provider:` prefix."""
    return model.partition(":")[2] if ":" in model else model


def _forms(model: str) -> list[str]:
    model = model.strip()
    undated = _SNAPSHOT_SUFFIX.sub("", model)
    return [model] if undated == model else [model, undated]


def _candidates(table: RatesTable, reported: Optional[str],
                run_model: Optional[str]) -> list[ModelRates]:
    """The rates entries a model request may have used, by the first rule
    that finds any:

    1. the reported model string itself (or without its snapshot date);
    2. the run's model, when the reported id is its id without the
       `provider:` prefix (`anthropic:claude-x` ↔ reported `claude-x`);
    3. the reported id under the run model's provider prefix (a fallback
       model of the same provider);
    4. every entry whose id without the prefix is the reported id (a harness
       reports the API model behind its selector).

    With no reported model, the run's model stands in: the exact string, its
    bare id, then rule 4 for its bare id."""
    if reported and reported.strip():
        forms = _forms(reported)
        found = [table[f] for f in forms if f in table]
        if found:
            return found
        if run_model:
            if run_model in table and _bare(run_model) in forms:
                return [table[run_model]]
            if ":" in run_model:
                provider = run_model.partition(":")[0]
                found = [table[f"{provider}:{f}"] for f in forms if f"{provider}:{f}" in table]
                if found:
                    return found
        return [rates for key, rates in table.items() if ":" in key and _bare(key) in forms]
    if not run_model:
        return []
    if run_model in table:
        return [table[run_model]]
    forms = _forms(_bare(run_model))
    found = [table[f] for f in forms if f in table]
    if found:
        return found
    return [rates for key, rates in table.items() if ":" in key and _bare(key) in forms]


def budget_weights(
    table: Optional[RatesTable], reported: Optional[str], run_model: Optional[str],
    *, writes: bool = False,
) -> BudgetWeights:
    """The weights for one model request (or a run's unbroken-down usage),
    under a configured source's table.

    FULL_WEIGHTS when there is no matching entry or a matching entry whose
    input rate is 0. When several entries match, each weight is the highest
    among them, so a budget never undercounts. `writes`: the slice has cache
    writes, so a missing write rate (counted 1:1) is worth a warning."""
    model = (reported or "").strip() or (run_model or "")
    candidates = _candidates(table or {}, reported, run_model)
    weights = [weights_for(rates) for rates in candidates]
    if not weights or any(w is None for w in weights):
        _warn_once(f"model:{model}", "no usable model rates for %r (unlisted, or rated "
                   "at zero); its cache reads count in full in budgets", model or "unknown")
        return FULL_WEIGHTS
    if writes and any(rates.cache_write is None for rates in candidates):
        _warn_once(f"write:{model}", "model rates for %r have no cache_write rate; its "
                   "cache writes count 1:1 in budgets", model or "unknown")
    return BudgetWeights(
        max(w.cache_read for w in weights),  # type: ignore[union-attr]
        max(w.cache_write for w in weights),  # type: ignore[union-attr]
        max(w.cache_write_1h for w in weights),  # type: ignore[union-attr]
    )
