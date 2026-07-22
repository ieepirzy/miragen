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