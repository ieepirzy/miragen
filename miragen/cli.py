from __future__ import annotations

import importlib.util
import logging
import os
from pathlib import Path

import click
import uvicorn
from pydantic import ValidationError

from miragen.load import load_profile

logger = logging.getLogger(__name__)


def _import_tools(tools: str) -> None:
    """
    Import a tools module by path or module name, triggering all @register calls.
    Silently skips if the file doesn't exist — tools are optional.
    """
    path = Path(f"{tools}.py")

    if path.exists():
        spec = importlib.util.spec_from_file_location("_user_tools", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        click.echo(f"Loaded tools from {path}")
    else:
        click.echo(f"No tools file found at {path}, starting without local tools")


@click.group()
def cli():
    """Agent runner."""
    pass


@cli.command()
@click.option("--tools", default="tools", envvar="TOOLS",
              help="Tools module to import (default: tools.py)")
@click.option("--host", default="0.0.0.0", envvar="HOST", show_default=True)
@click.option("--port", default=8000, envvar="PORT", show_default=True)
@click.option("--reload", is_flag=True, default=False,
              help="Enable auto-reload (development only)")
def run(tools: str, host: str, port: int, reload: bool) -> None:
    """Start the agent container server."""
    _import_tools(tools)
    uvicorn.run(
        "miragen.app:app",
        host=host,
        port=port,
        reload=reload,
    )


@cli.command()
def contract() -> None:
    """Print the profile contract levels this runtime supports, as JSON.

    The third declaration surface next to the image label and /health (#75):
    lets a daemon or an operator query any runtime — container or venv —
    without booting an agent."""
    import json as _json

    from miragen.profile_contract import SUPPORTED_PROFILE_CONTRACTS

    click.echo(_json.dumps({"profile_contracts": list(SUPPORTED_PROFILE_CONTRACTS)}))


@cli.command(name="codex-login")
@click.option(
    "--codex-home", default=None, envvar="CODEX_HOME",
    help="Codex credential store to populate (default: /agent/codex-home). Point this at "
         "the SHARED volume every codex agent container mounts (executor.codex_home).",
)
def codex_login(codex_home: str | None) -> None:
    """Authenticate Codex with ChatGPT ONCE, into a shared credential store.

    Runs the device-code flow (open the printed URL, enter the code). The
    credentials land in CODEX_HOME; mount that same volume into every codex
    agent container and they all reuse this one login — agent containers never
    authenticate themselves. Re-run to refresh. See docs/design/codex-auth.md.
    """
    home = codex_home or "/agent/codex-home"
    os.environ["CODEX_HOME"] = home
    Path(home).mkdir(parents=True, exist_ok=True)

    try:
        from openai_codex import Codex
    except ImportError:
        click.echo(click.style(
            "The codex extra is not installed — run `pip install miragen[codex]`.", fg="red"))
        raise SystemExit(1)

    click.echo(f"Authenticating Codex into {home} …")
    try:
        with Codex() as codex:
            handle = codex.login_chatgpt_device_code()
            click.echo("")
            click.echo(click.style("  Open:  ", bold=True) + handle.verification_url)
            click.echo(click.style("  Code:  ", bold=True) + handle.user_code)
            click.echo("")
            click.echo("Waiting for you to approve in the browser (Ctrl-C to cancel) …")
            try:
                handle.wait()
            except KeyboardInterrupt:
                handle.cancel()
                click.echo(click.style("Login cancelled.", fg="yellow"))
                raise SystemExit(1)
    except SystemExit:
        raise
    except Exception as e:
        click.echo(click.style(f"✗ Login failed: {e}", fg="red"))
        raise SystemExit(1)

    click.echo(click.style(f"✓ Codex authenticated — credentials written to {home}", fg="green"))
    click.echo("Mount this volume at executor.codex_home in every codex agent container.")


@cli.command(name="kimi-login")
@click.option(
    "--kimi-home", default=None, envvar="KIMI_CODE_HOME",
    help="Kimi Code home to populate (default: /agent/kimi-home). Point this at "
         "the SHARED volume every kimi-code agent container mounts (executor.kimi_home).",
)
def kimi_login(kimi_home: str | None) -> None:
    """Authenticate Kimi Code ONCE into a shared home (subscription path).

    Sets KIMI_CODE_HOME and runs the product's non-interactive device-code
    login (`kimi login`). Mount that same volume at executor.kimi_home in every
    kimi-code agent container — agents never log in themselves. See
    docs/design/subscription-homes.md.
    """
    import shutil
    import subprocess

    home = kimi_home or "/agent/kimi-home"
    os.environ["KIMI_CODE_HOME"] = home
    Path(home).mkdir(parents=True, exist_ok=True)

    kimi_bin = shutil.which("kimi")
    if not kimi_bin:
        click.echo(click.style(
            "The `kimi` CLI was not found on PATH. Install Kimi Code CLI "
            "(https://github.com/MoonshotAI/kimi-code) and re-run, or set "
            "KIMI_API_KEY/MOONSHOT_API_KEY for the metered path.",
            fg="red",
        ))
        raise SystemExit(1)

    click.echo(f"Authenticating Kimi Code into {home} …")
    click.echo("(device-code flow: open the printed URL and enter the code)")
    try:
        result = subprocess.run(
            [kimi_bin, "login"],
            env={**os.environ, "KIMI_CODE_HOME": home},
            check=False,
        )
    except KeyboardInterrupt:
        click.echo(click.style("Login cancelled.", fg="yellow"))
        raise SystemExit(1)

    if result.returncode != 0:
        click.echo(click.style(f"✗ kimi login exited with code {result.returncode}", fg="red"))
        raise SystemExit(1)

    click.echo(click.style(f"✓ Kimi authenticated — credentials written under {home}", fg="green"))
    click.echo("Mount this volume at executor.kimi_home in every kimi-code agent container.")


@cli.command(name="grok-login")
@click.option(
    "--grok-home", default=None, envvar="GROK_HOME",
    help="Grok Build home to populate (default: /agent/grok-home). Point this at "
         "the SHARED volume every grok-build agent container mounts (executor.grok_home).",
)
def grok_login(grok_home: str | None) -> None:
    """Authenticate Grok Build ONCE into a shared home (subscription path).

    Sets GROK_HOME and runs `grok login --device-auth` (device-code flow for
    headless/remote hosts). Mount that same volume at executor.grok_home in
    every grok-build agent container — agents never log in themselves. See
    docs/design/subscription-homes.md.
    """
    import shutil
    import subprocess

    home = grok_home or "/agent/grok-home"
    os.environ["GROK_HOME"] = home
    Path(home).mkdir(parents=True, exist_ok=True)

    grok_bin = os.environ.get("GROK_BIN") or shutil.which("grok")
    if not grok_bin:
        click.echo(click.style(
            "The `grok` CLI was not found on PATH. Install Grok Build "
            "(https://docs.x.ai/build/overview) and re-run, or set XAI_API_KEY "
            "for the metered path.",
            fg="red",
        ))
        raise SystemExit(1)

    click.echo(f"Authenticating Grok Build into {home} …")
    click.echo("(device-code flow: open the printed URL and enter the code)")
    try:
        result = subprocess.run(
            [grok_bin, "login", "--device-auth"],
            env={**os.environ, "GROK_HOME": home},
            check=False,
        )
    except KeyboardInterrupt:
        click.echo(click.style("Login cancelled.", fg="yellow"))
        raise SystemExit(1)

    if result.returncode != 0:
        click.echo(click.style(f"✗ grok login exited with code {result.returncode}", fg="red"))
        raise SystemExit(1)

    click.echo(click.style(f"✓ Grok authenticated — credentials written under {home}", fg="green"))
    click.echo("Mount this volume at executor.grok_home in every grok-build agent container.")


@cli.command()
@click.argument("profile", envvar="AGENT_PROFILE", default="agent.yaml")
@click.option("--tools", default="tools", envvar="TOOLS",
              help="Tools module to import before validating")
def validate(profile: str, tools: str) -> None:
    """
    Validate an agent profile YAML without starting the server.
    Useful in CI or when authoring a new agent.
    """
    _import_tools(tools)

    try:
        p = load_profile(profile)
        click.echo(click.style(f"✓ '{p.name}' is valid", fg="green"))
        click.echo(f"  mode:         {p.mode}")
        if p.is_executor:
            click.echo(f"  executor:     {p.executor.executor} (sandbox: {p.executor.sandbox_mode}, "
                       f"approval: {p.executor.approval_policy})")
            click.echo(f"  mcp servers:  {[s.name for s in p.executor.mcp_servers or []]}")
        else:
            click.echo(f"  model:        {p.spec.model}")
            click.echo(f"  capabilities: {p.spec.capabilities or []}")
        click.echo(f"  triggers:     {[t.type for t in p.triggers]}")
        click.echo(f"  tools:        {p.tools or []}")
    except ValidationError as e:
        click.echo(click.style(f"✗ Invalid profile — {e.error_count()} error(s):", fg="red"))
        for err in e.errors():
            loc = ".".join(str(part) for part in err["loc"]) or "<root>"
            msg = err["msg"]
            if err["type"] == "extra_forbidden":
                msg = "unknown field — check spelling against the profile reference in the README"
            click.echo(f"  {loc}: {msg}")
        raise SystemExit(1)
    except Exception as e:
        click.echo(click.style(f"✗ Invalid profile: {e}", fg="red"))
        raise SystemExit(1)

@cli.command(name="memory-hook")
@click.argument("harness", type=click.Choice(["claude-code", "codex"]))
def memory_hook(harness: str) -> None:
    """Hook bridge (§18.7): read ONE harness hook event JSON on stdin,
    capture it durably / answer context, exit.

    Installed into harness hook configuration by miragen itself (Codex
    hooks.json; external Claude Code sessions can point their settings at
    it). Credentials and identity come from the trusted host environment
    (AGENT_PROFILE + the profile's memory env vars) — never from the
    payload. Fail-open: any failure logs to stderr and exits 0, because a
    broken memory service must not block the agent's actual work.
    """
    import asyncio
    import json as _json
    import sys

    from miragen.memory import MemoryClient, MemoryLifecycle
    from miragen.memory.harness_hooks import handle_hook_event, normalize_hook_payload

    if os.environ.get("MIRAGEN_WORKER"):
        return  # a memory model call miragen started: never captured (claude_code.py)
    try:
        payload = _json.load(sys.stdin)
        profile = load_profile(os.environ.get("AGENT_PROFILE", "agent.yaml"))
        if profile.memory is None:
            return  # memory not enabled: hook is a no-op, not an error
        lifecycle = MemoryLifecycle(
            profile.memory, profile.name, MemoryClient(profile.memory)
        )
        event = normalize_hook_payload(harness, payload)
        if event is None:
            return
        instance = os.environ.get("MIRAGEN_MEMORY_INSTANCE") or None

        async def _run():
            # Explicit bound (§18.7) UNDER the harness-side hook timeout,
            # so the bridge gives up before the harness gives up on it.
            return await asyncio.wait_for(
                handle_hook_event(lifecycle, event, instance=instance), timeout=8
            )

        output = asyncio.run(_run())
        if output is not None:
            click.echo(_json.dumps(output))
    except Exception as exc:  # noqa: BLE001 — fail-open by contract
        print(f"miragen memory-hook: {exc}", file=sys.stderr)


@cli.command(name="memory-worker")
@click.option("--once", is_flag=True, default=False,
              help="One sweep, then exit (default: loop forever).")
@click.option("--interval", default=30, show_default=True,
              help="Seconds between sweeps when looping.")
@click.option("--limit", default=5, show_default=True,
              help="Jobs claimed per sweep.")
@click.option("--embed-url", envvar="MIRAGEN_MEMORY_EMBED_URL", default=None,
              help="Embed endpoint (POST /embed); enables index-job backfill.")
@click.option("--lease", "lease_seconds", default=600, show_default=True,
              help="Job lease in seconds. Jobs in a sweep run one after another, so keep "
                   "it above limit × the slowest extraction (a claude-code: extraction "
                   "plus its checks can take a minute).")
@click.option("--max-backoff", default=900, show_default=True,
              help="Upper bound (seconds) of the pause after sweeps where every job failed.")
@click.option("--skip-before", envvar="MIRAGEN_WORKER_SKIP_BEFORE", default=None,
              help="ISO timestamp: jobs for events received earlier are completed without "
                   "a model call. Every captured event queues a job, so a first deployment "
                   "would otherwise extract the whole history on the model's quota.")
@click.option("--principal", envvar="MIRAGEN_WORKER_PRINCIPAL", default=None,
              help="The worker's Loimi principal. With --token-file and LOIMI_OPERATOR_TOKEN, "
                   "it is created (or a token minted) on first start and the token kept in "
                   "the file; the bridge's scopes.worker_principal grants it per scope.")
@click.option("--token-file", envvar="MIRAGEN_WORKER_TOKEN_FILE", default=None,
              help="Where the worker's own principal token is kept (0600).")
def memory_worker(once: bool, interval: int, limit: int, embed_url: str | None,
                  lease_seconds: int, max_backoff: int, skip_before: str | None,
                  principal: str | None, token_file: str | None) -> None:
    """The bounded extraction worker (memory pass PR 3, §17.5): claims
    consolidate jobs through /memory/v1 as its own maintain-capable
    principal and proposes extracted memories through the same admission
    door as every other principal — no database credential involved.

    Requires the profile's memory.extraction.enabled and a model
    (memory.extraction.model, defaulting to the profile's spec.model).
    """
    import asyncio
    import time as _time

    from miragen.memory import MemoryClient
    from miragen.memory.extraction import (
        build_http_embedder,
        build_model_checker,
        build_model_extractor,
        run_worker_once,
    )

    profile = load_profile(os.environ.get("AGENT_PROFILE", "agent.yaml"))
    if profile.memory is None or not profile.memory.extraction.enabled:
        raise click.ClickException(
            "memory.extraction.enabled is not set on this profile — the "
            "worker only runs where the deployment explicitly enabled it"
        )
    model = profile.memory.extraction.model or (
        profile.spec.model if profile.spec else None
    )
    if not model:
        raise click.ClickException(
            "no extraction model: set memory.extraction.model (required on "
            "executor-tier profiles, which have no spec.model)"
        )

    if principal and token_file:
        try:
            how = asyncio.run(ensure_worker_token(profile.memory, principal, Path(token_file)))
        except Exception as exc:  # noqa: BLE001 — surfaced as a CLI error
            raise click.ClickException(f"worker principal {principal}: {exc}") from exc
        click.echo(f"{utc_stamp()} worker principal {principal}: token {how}")

    client = MemoryClient(profile.memory)
    extract = build_model_extractor(model)
    check = build_model_checker(model)
    embed = build_http_embedder(embed_url) if embed_url else None

    cutoff = None
    if skip_before:
        from datetime import UTC, datetime

        try:
            cutoff = datetime.fromisoformat(skip_before.replace("Z", "+00:00"))
        except ValueError as exc:
            raise click.ClickException(f"--skip-before: not an ISO timestamp: {skip_before}") from exc
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=UTC)

    click.echo(f"{utc_stamp()} worker started: model {model}, limit {limit}, interval {interval}s"
               + (f", skipping events received before {skip_before}" if cutoff else ""))
    backoff = 0
    while True:
        results = asyncio.run(
            run_worker_once(client, extract=extract, check=check, embed=embed,
                            limit=limit, lease_seconds=lease_seconds, skip_before=cutoff)
        )
        for line in sweep_lines(results):
            click.echo(f"{utc_stamp()} {line}")
        if once:
            break
        if drain_backlog(results, limit=limit):
            continue  # skipping costs no model call: don't pace it like work
        backoff = next_backoff(backoff, results, interval=interval, ceiling=max_backoff)
        _time.sleep(backoff or interval)


def utc_stamp() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def drain_backlog(results: list[dict], *, limit: int) -> bool:
    """A full sweep that only skipped pre-cutoff backlog made no model call,
    so the next sweep runs at once instead of after `interval`: the backlog
    drains at claim speed and new jobs aren't queued behind it for hours."""
    return len(results) >= limit and all(r.get("status") == "skipped_backlog" for r in results)


def sweep_lines(results: list[dict]) -> list[str]:
    """One summary line for the skipped backlog of a sweep (with the span of
    event times, so progress through the backlog is visible), one line per
    processed or failed job with its event's time, source kind and scope."""
    lines = []
    skipped = [r for r in results if r.get("status") == "skipped_backlog"]
    if skipped:
        stamps = sorted(r["event_at"] for r in skipped if r.get("event_at"))
        span = f": events {stamps[0]} .. {stamps[-1]}" if stamps else ""
        lines.append(f"skipped {len(skipped)} backlog job(s){span}")
    for r in results:
        status = r.get("status")
        if status == "skipped_backlog":
            continue
        where = (f"{r.get('source_kind') or '?'} event {r.get('event_at') or '?'}"
                 f" in {r.get('scope_id') or '?'}")
        if status == "done":
            outcome = ("nothing to extract" if r.get("skipped") else
                       f"+{r.get('accepted', 0)} accepted, {r.get('quarantined', 0)} quarantined,"
                       f" {len(r.get('dropped', []))} dropped")
            lines.append(f"job {r.get('job_id')} done ({where}): {outcome}")
        else:
            lines.append(f"job {r.get('job_id')} {status}"
                         + (f" ({where})" if r.get("event_at") else "")
                         + (f": {str(r.get('error'))[:300]}" if r.get("error") else ""))
    return lines


async def ensure_worker_token(
    spec, principal: str, token_file: Path, *, environ: dict | None = None,
    client_factory=None,
) -> str:
    """The worker's own principal token into `spec.credential_env`: already
    set → kept; the token file → read; else created (or re-minted when the
    principal exists) with the operator token, persisted 0600. Returns how."""
    from miragen.memory import MemoryClient
    from miragen.memory.client import MemoryAPIError

    env = os.environ if environ is None else environ
    if env.get(spec.credential_env):
        return "from environment"
    if token_file.exists():
        env[spec.credential_env] = token_file.read_text().strip()
        return "from file"
    operator = env.get("LOIMI_OPERATOR_TOKEN")
    if not operator:
        raise RuntimeError("no token file yet and LOIMI_OPERATOR_TOKEN is not set")
    factory = client_factory or (lambda token: MemoryClient(spec, token=token))
    admin = factory(operator)
    try:
        created = await admin.admin_create_principal(
            principal_id=principal, kind="agent",
            description="miragen memory-worker (extraction; maintain on granted scopes)",
        )
        token, how = created["token"], "created"
    except MemoryAPIError as exc:
        if exc.status_code != 409:
            raise
        token, how = (await admin.admin_mint_token(principal_id=principal))["token"], "minted"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(token + "\n")
    env[spec.credential_env] = token
    return how


def next_backoff(current: int, results: list[dict], *, interval: int, ceiling: int) -> int:
    """Failed jobs go straight back to pending (Loimi has no retry delay), so
    a model outage — a subscription rate limit above all — would otherwise
    re-run every job each sweep. A sweep where every job failed doubles the
    pause (from `interval`, up to `ceiling`); any success resets it."""
    if not results or any(r.get("status") != "failed" for r in results):
        return 0
    return min(ceiling, max(interval, current * 2))


@cli.command(name="memory-conformance")
@click.argument("base_url", required=False)
@click.option("--operator-token", envvar="MEMORY_OPERATOR_TOKEN", default=None,
              help="The implementation's operator credential (provisions fixtures).")
@click.option("--ephemeral", "self_test", is_flag=True, default=False,
              help="Run against the built-in ephemeral backend (self-test).")
def memory_conformance(base_url: str | None, operator_token: str | None,
                       self_test: bool) -> None:
    """Run the memory backend protocol conformance suite
    (docs/memory-backend-protocol.md) against BASE_URL — or against the
    built-in ephemeral backend with --ephemeral. Exit 0 only on full pass."""
    import asyncio

    from miragen.memory.conformance import run_conformance

    if self_test:
        from miragen.memory.ephemeral import EphemeralMemoryService

        service = EphemeralMemoryService()
        results = asyncio.run(run_conformance(
            base_url="http://ephemeral.local", operator_token=service.operator_token,
            transport=service.transport(),
        ))
    else:
        if not base_url or not operator_token:
            raise click.ClickException(
                "BASE_URL and --operator-token are required (or use --ephemeral)"
            )
        results = asyncio.run(run_conformance(
            base_url=base_url, operator_token=operator_token,
        ))

    failed = [r for r in results if not r.passed]
    for result in results:
        mark = "PASS" if result.passed else "FAIL"
        click.echo(f"[{mark}] {result.name}: {result.detail}")
    click.echo(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        raise SystemExit(1)
