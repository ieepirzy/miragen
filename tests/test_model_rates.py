"""The model rates adapter: document parsing, the file and HTTP sources, the
on-disk last good copy, and every failure mode keeping the last good rates."""

import json
import logging
import os
import time
from fractions import Fraction

import httpx
import pytest

from miragen import model_rates
from miragen.model_rates import (
    FileRatesSource,
    HttpRatesSource,
    RatesDocumentInvalid,
    parse_rates_document,
    source_from_env,
    weights_for,
)

DOC = {"models": {
    "anthropic:claude-x": {"input": "2.00", "cached_input": "0.10", "cache_write": "2.50",
                           "cache_write_1h": "4.00", "output": "10.00"},
    "deepseek:ds": {"input": "0.15", "cached_input": "0.003", "output": "0.60"},
}}


# ── parsing ─────────────────────────────────────────────────────────────────


def test_a_document_parses_to_exact_rates():
    table = parse_rates_document(DOC)
    assert table["anthropic:claude-x"].cached_input == Fraction(1, 10)
    assert table["anthropic:claude-x"].cache_write_1h == 4
    assert table["deepseek:ds"].cache_write is None
    weights = weights_for(table["deepseek:ds"])
    assert weights.cache_read == Fraction(1, 50)
    assert weights.cache_write == 1 and weights.cache_write_1h == 1


def test_unknown_fields_are_ignored():
    doc = {"currency": "XYZ", "generated_at": "now", "models": {
        "p:m": {"input": "1", "cached_input": "0.5", "output": "2", "tiers": [], "note": "x"}}}
    assert set(parse_rates_document(doc)) == {"p:m"}


def test_malformed_entries_are_skipped_with_a_log_line(caplog):
    doc = {"models": {
        "ok:m": {"input": "1", "cached_input": "0.1", "output": "1"},
        "missing:m": {"input": "1", "output": "1"},
        "negative:m": {"input": "-1", "cached_input": "0", "output": "1"},
        "text:m": {"input": "lots", "cached_input": "0", "output": "1"},
        "bool:m": {"input": True, "cached_input": "0", "output": "1"},
        "nan:m": {"input": "NaN", "cached_input": "0", "output": "1"},
        "badwrite:m": {"input": "1", "cached_input": "0", "output": "1", "cache_write": "x"},
        "list:m": ["1", "2"],
        "": {"input": "1", "cached_input": "0", "output": "1"},
    }}
    with caplog.at_level(logging.WARNING, logger="miragen.model_rates"):
        table = parse_rates_document(doc)
    assert set(table) == {"ok:m"}
    assert sum("skipping" in r.getMessage() for r in caplog.records) == 8


def test_numbers_are_accepted_as_well_as_strings():
    table = parse_rates_document({"models": {"p:m": {"input": 2, "cached_input": 0.5,
                                                      "output": 4}}})
    assert table["p:m"].cached_input == Fraction(1, 2)


@pytest.mark.parametrize("doc", [None, [], {}, {"models": []}, {"models": "x"}])
def test_a_document_without_a_models_object_is_invalid(doc):
    with pytest.raises(RatesDocumentInvalid):
        parse_rates_document(doc)


def test_an_empty_models_object_is_a_valid_empty_table():
    assert parse_rates_document({"models": {}}) == {}


def test_a_zero_input_rate_cannot_weight():
    assert weights_for(parse_rates_document(
        {"models": {"s:m": {"input": "0", "cached_input": "0", "output": "0"}}})["s:m"]) is None
    assert weights_for(None) is None


# ── file source ─────────────────────────────────────────────────────────────


def test_the_file_source_rereads_on_change_and_keeps_the_last_good_copy(tmp_path):
    path = tmp_path / "rates.json"
    source = FileRatesSource(path)
    assert source.current() is None  # not there yet
    path.write_text(json.dumps(DOC))
    assert set(source.current()) == set(DOC["models"])
    path.write_text("{not json")
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert set(source.current()) == set(DOC["models"])  # last good kept
    path.write_text(json.dumps({"models": {"p:m": {"input": "1", "cached_input": "0",
                                                   "output": "1"}}}))
    os.utime(path, (time.time() + 10, time.time() + 10))
    assert set(source.current()) == {"p:m"}
    path.unlink()
    assert set(source.current()) == {"p:m"}


# ── HTTP source ─────────────────────────────────────────────────────────────


class _Server:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, Exception):
            raise response
        return response


def _http(server, tmp_path, **kw):
    return HttpRatesSource("https://catalogue.example/api/model-rates", token="secret-token",
                           cache_path=tmp_path / "model-rates" / "rates.json",
                           transport=httpx.MockTransport(server), **kw)


def test_a_fetch_sends_the_bearer_token_and_stores_the_rates(tmp_path):
    server = _Server(httpx.Response(200, json=DOC))
    source = _http(server, tmp_path)
    assert source.current() is None
    assert source.refresh() is True
    assert set(source.current()) == set(DOC["models"])
    assert server.requests[0].headers["Authorization"] == "Bearer secret-token"


def test_a_restart_without_network_has_the_last_good_rates(tmp_path):
    _http(_Server(httpx.Response(200, json=DOC)), tmp_path).refresh()
    offline = _http(_Server(httpx.ConnectError("no route")), tmp_path)
    assert set(offline.current()) == set(DOC["models"])  # loaded from disk
    assert offline.refresh() is False
    table = offline.current()
    assert table["anthropic:claude-x"].cache_write_1h == 4  # round-trips exactly
    assert table["deepseek:ds"].cached_input == Fraction(3, 1000)


@pytest.mark.parametrize("failure", [
    httpx.ConnectError("no route"),
    httpx.ReadTimeout("slow"),
    httpx.Response(401, json={"detail": "no"}),
    httpx.Response(503, text="down"),
    httpx.Response(200, text="<html>not json</html>"),
    httpx.Response(200, json={"rates": {}}),
    httpx.Response(200, content=b"x" * (model_rates.MAX_DOCUMENT_BYTES + 1)),
])
def test_a_failed_fetch_keeps_the_last_good_rates(tmp_path, failure):
    server = _Server(httpx.Response(200, json=DOC), failure)
    source = _http(server, tmp_path)
    assert source.refresh() is True
    assert source.refresh() is False
    assert set(source.current()) == set(DOC["models"])
    on_disk = json.loads((tmp_path / "model-rates" / "rates.json").read_text())
    assert set(on_disk["models"]) == set(DOC["models"])


def test_a_failure_streak_logs_once(tmp_path, caplog):
    source = _http(_Server(httpx.ConnectError("no route")), tmp_path)
    with caplog.at_level(logging.WARNING, logger="miragen.model_rates"):
        for _ in range(3):
            source.refresh()
    assert sum("fetch from" in r.getMessage() for r in caplog.records) == 1


def test_a_corrupt_last_good_copy_is_ignored(tmp_path):
    cache = tmp_path / "model-rates" / "rates.json"
    cache.parent.mkdir(parents=True)
    cache.write_text("{broken")
    assert _http(_Server(httpx.ConnectError("x")), tmp_path).current() is None


def test_the_background_refresh_starts_and_stops(tmp_path):
    server = _Server(httpx.Response(200, json=DOC))
    source = _http(server, tmp_path)
    source.start()
    try:
        deadline = time.monotonic() + 5
        while source.current() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert source.current() is not None
    finally:
        source.stop()
    assert source._thread is None


def test_the_refresh_interval_has_a_floor(tmp_path):
    assert _http(_Server(httpx.Response(200, json=DOC)), tmp_path, refresh_s=1).refresh_s == 30


# ── configuration ───────────────────────────────────────────────────────────


def test_nothing_configured_is_no_source(tmp_path):
    assert source_from_env({}, tmp_path) is None


def test_the_url_configures_an_http_source(tmp_path):
    source = source_from_env({
        "MIRAGEN_MODEL_RATES_URL": "https://catalogue.example/rates",
        "MIRAGEN_MODEL_RATES_TOKEN": "t0k3n",
        "MIRAGEN_MODEL_RATES_REFRESH_S": "600",
    }, tmp_path)
    assert isinstance(source, HttpRatesSource)
    assert source.url == "https://catalogue.example/rates"
    assert source._token == "t0k3n" and source.refresh_s == 600
    assert source._cache_path == tmp_path / "rates.json"


def test_the_file_configures_a_file_source(tmp_path):
    source = source_from_env({"MIRAGEN_MODEL_RATES_PATH": str(tmp_path / "r.json")}, tmp_path)
    assert isinstance(source, FileRatesSource)


def test_the_url_wins_over_the_file(tmp_path):
    source = source_from_env({"MIRAGEN_MODEL_RATES_URL": "http://c.example/r",
                              "MIRAGEN_MODEL_RATES_PATH": "/x.json"}, tmp_path)
    assert isinstance(source, HttpRatesSource)


@pytest.mark.parametrize("env", [
    {"MIRAGEN_MODEL_RATES_URL": "file:///etc/passwd"},
    {"MIRAGEN_MODEL_RATES_URL": "https://c.example/r", "MIRAGEN_MODEL_RATES_REFRESH_S": "soon"},
])
def test_bad_configuration_stops_startup(env, tmp_path):
    with pytest.raises(ValueError):
        source_from_env(env, tmp_path)


# ── startup and deadlines ───────────────────────────────────────────────────


def test_start_without_a_copy_on_disk_fetches_before_returning(tmp_path):
    source = _http(_Server(httpx.Response(200, json=DOC)), tmp_path)
    source.start()
    try:
        assert set(source.current()) == set(DOC["models"])  # no waiting needed
    finally:
        source.stop()


def test_start_with_a_copy_on_disk_does_not_wait_for_the_network(tmp_path):
    _http(_Server(httpx.Response(200, json=DOC)), tmp_path).refresh()
    server = _Server(httpx.Response(200, json=DOC))
    source = _http(server, tmp_path)
    source.start()
    try:
        assert source.current() is not None
        assert server.requests == []  # the next fetch waits for the interval
    finally:
        source.stop()


def _trickle(chunks: int, delay: float):
    def body():
        for _ in range(chunks):
            time.sleep(delay)
            yield b" "
        yield json.dumps(DOC).encode()
    return body()


def test_a_fetch_has_one_wall_clock_deadline_across_phases(tmp_path):
    # Each chunk arrives well inside httpx's per-read timeout; the whole
    # body does not arrive inside the total deadline.
    def handler(request):
        return httpx.Response(200, content=_trickle(8, 0.1))

    source = HttpRatesSource("https://catalogue.example/r", transport=httpx.MockTransport(handler))
    started = time.monotonic()
    assert source.refresh(timeout_s=0.35) is False
    assert time.monotonic() - started < 0.7
    assert source.current() is None
    assert source.refresh(timeout_s=5) is True  # the same body, given time


def test_startup_against_an_unreachable_source_is_bounded(tmp_path):
    def handler(request):
        return httpx.Response(200, content=_trickle(50, 0.1))

    source = HttpRatesSource("https://catalogue.example/r", transport=httpx.MockTransport(handler),
                             cache_path=tmp_path / "rates.json")
    started = time.monotonic()
    source.start(startup_timeout_s=0.3)
    try:
        assert time.monotonic() - started < 1.0
        assert source.current() is None
    finally:
        source.stop()


def test_the_path_variable_is_not_a_secret_file_name():
    # `*_FILE` is consumed by the startup secret loader (miragen.app).
    assert not model_rates.PATH_ENV.endswith("_FILE")
