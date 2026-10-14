"""A stable rendering of the Claude Code harness's launch options: the CLI
argv the SDK would build, the child environment, the hook table and the exec
wrapper. Shared by the locked-posture golden test (the default must stay
byte-identical) and the engineer-posture tests.

Regenerate the golden (only when a locked-posture change is intended):
    PYTHONDONTWRITEBYTECODE=1 python -m tests.claude_code_snapshot > tests/fixtures/claude_code_locked_options.json
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections import deque
from pathlib import Path
from unittest import mock

from miragen.harness.claude_code import WRAPPER_FILE, ClaudeCodeHarness, ClaudeCodeSettings
from miragen.harness.gateway import ToolGateway
from miragen.models import AgentProfile

# The parent environment every snapshot is taken under.
PARENT_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "CLAUDE_CODE_OAUTH_TOKEN": "sk-sub",
              "ANTHROPIC_API_KEY": "sk-api", "UPSTREAM_SECRET": "s3cret"}

LOCKED_PROFILES = {
    "plain": {},
    "web_and_gated": {"capabilities": ["WebSearch", "WebFetch"],
                      "approval_required": ["gw_*"]},
}


def profile(variant: dict, **spec_extra) -> AgentProfile:
    variant = dict(variant)
    caps = variant.pop("capabilities", None)
    return AgentProfile.model_validate({
        "name": "mira", "mode": "interactive", "triggers": [{"type": "http"}],
        "inject_timestamp": False,
        "spec": {"model": "claude-code:sonnet", "instructions": "You are Mira.",
                 **({"capabilities": caps} if caps else {}), **spec_extra},
        **variant,
    })


def render(harness: ClaudeCodeHarness, options, root: Path, *, argv: bool = True) -> dict:
    """What miragen passes to the SDK (option values, env, hooks, wrapper).
    ``argv`` adds the CLI command line the SDK would build from them: a
    private SDK API whose rendering changes between SDK releases, so the
    golden leaves it out and only targeted assertions read it."""

    def norm(value):
        if isinstance(value, str):
            value = value.replace(str(root), "<root>").replace(sys.executable, "<python>")
            token = harness.gateway.credential("chat")
            return value.replace(token, "<gateway-token>")
        if isinstance(value, list):
            return [norm(v) for v in value]
        if isinstance(value, dict):
            return {k: norm(v) for k, v in value.items()}
        return value

    hooks = {event: [{"matcher": m.matcher, "hooks": len(m.hooks)} for m in matchers]
             for event, matchers in (options.hooks or {}).items()}
    view = {
        "env": dict(sorted(options.env.items())),
        "hooks": hooks,
        "permission_mode": options.permission_mode,
        "can_use_tool": options.can_use_tool is not None,
        "cwd": options.cwd,
        "options": {k: getattr(options, k) for k in (
            "tools", "allowed_tools", "disallowed_tools", "system_prompt", "setting_sources",
            "skills", "plugins", "strict_mcp_config", "add_dirs", "verbatim_prompts",
            "mcp_servers", "model", "effort", "cli_path", "resume", "session_id")},
        "wrapper": (harness.settings.claude_home / WRAPPER_FILE).read_text(),
    }
    if argv:
        from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

        view["argv"] = SubprocessCLITransport("", options)._build_command()
    return norm(view)


def locked_snapshots() -> dict:
    out = {}
    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, PARENT_ENV, clear=True):
        root = Path(tmp)
        for name, variant in LOCKED_PROFILES.items():
            prof = profile(variant)
            settings = ClaudeCodeSettings(claude_home=root / "home", workdirs=root / "work",
                                          gateway_url="http://127.0.0.1:9/mcp/gateway/",
                                          cli_path="/usr/bin/env")
            harness = ClaudeCodeHarness(prof, ToolGateway(
                prof, native_capabilities=ClaudeCodeHarness.native_capabilities), settings)
            options = harness._options("chat", root / "work" / "chat", resume=None,
                                       new_id="00000000-0000-0000-0000-000000000000",
                                       stderr=deque())
            out[name] = render(harness, options, root, argv=False)
    return out


if __name__ == "__main__":
    print(json.dumps(locked_snapshots(), indent=1, sort_keys=True))
