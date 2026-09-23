"""Memory model calls through headless Claude Code (`claude -p`).

The selector (§17.7), extractor and checker (§17.5) are each one bounded
structured-output call. With a model string `claude-code:<model>` (e.g.
`claude-code:haiku`) they run through the `claude` binary on the operator's
Claude subscription instead of a metered API key
(docs/design/memory-effectiveness.md P1.0).

Contract, all of it load-bearing:

- **Isolated.** No settings sources (so no plugins and none of their hooks),
  no tools, no MCP servers, no session persistence, a replaced system prompt
  and an empty working directory (no CLAUDE.md, no auto-memory).
  `--bare` is NOT usable: it never reads OAuth, so it cannot use a
  subscription.
- **Never re-captured.** `MIRAGEN_WORKER=1` is set for the child; the hook
  adapter exits on it. Without that, every memory call would be captured as
  a harness session → episode → extraction → another call.
- **Subscription only.** API credentials outrank the subscription token in
  Claude Code's precedence, so they are scrubbed from the child's
  environment; a stray `ANTHROPIC_API_KEY` in a deployment must not silently
  switch this to per-token billing.
- **Failure is loud.** A non-zero exit, `is_error`, a timeout or a missing /
  invalid `structured_output` raises `ClaudeCodeError`. It is never read as
  "nothing selected" or "nothing durable": the selector degrades per §17.7
  and the extraction job stays retryable.
- **Bounded.** Every call holds a process-wide semaphore
  (`MIRAGEN_CLAUDE_CODE_CONCURRENCY`, default 2): each call is a Node process
  on a memory-constrained host.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile

from pydantic import BaseModel, ValidationError

PREFIX = "claude-code:"

# Credentials that outrank the subscription token (CLAUDE_CODE_OAUTH_TOKEN)
# or route the call to a metered provider. Never inherited by the child.
# (https://code.claude.com/docs/en/authentication, "Authentication
# precedence"). A gateway login or managed `forceLoginMethod` is host
# configuration, not environment: keep it off hosts that run the worker.
SCRUBBED_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_SERVICE_ACCOUNT_ID",
    "ANTHROPIC_WORKSPACE_ID",
    "ANTHROPIC_IDENTITY_TOKEN",
    "ANTHROPIC_IDENTITY_TOKEN_FILE",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)
WORKER_ENV = "MIRAGEN_WORKER"
DEFAULT_TIMEOUT_S = 120.0
_EMPTY_MCP = '{"mcpServers":{}}'

_semaphore: asyncio.Semaphore | None = None
_semaphore_loop: asyncio.AbstractEventLoop | None = None


class ClaudeCodeError(RuntimeError):
    """The call did not produce a valid structured result. Never 'empty'."""


class ClaudeCodeLimited(ClaudeCodeError):
    """The subscription's usage limit (or an API rate limit) refused the
    call. Transient by nature: retry later, never treat as 'nothing'."""


_LIMIT_MARKERS = ("usage limit", "session limit", "rate limit", "rate_limit", "hit your limit")


def _result_text(stdout: str) -> str:
    """Claude Code prints its JSON result even when it exits non-zero; the
    `result` field is the human-readable reason (e.g. the usage limit)."""
    try:
        data = json.loads(stdout)
    except ValueError:
        return ""
    return str(data.get("result") or "") if isinstance(data, dict) else ""


def is_claude_code_model(model: str | None) -> bool:
    return bool(model) and model.startswith(PREFIX)


def model_name(model: str) -> str:
    name = model.removeprefix(PREFIX)
    if not name:
        raise ValueError(f"no model after {PREFIX!r}: {model!r}")
    return name


def _limit() -> asyncio.Semaphore:
    """One semaphore per event loop (tests and the worker CLI create loops)."""
    global _semaphore, _semaphore_loop
    loop = asyncio.get_running_loop()
    if _semaphore is None or _semaphore_loop is not loop:
        size = max(1, int(os.environ.get("MIRAGEN_CLAUDE_CODE_CONCURRENCY", "2")))
        _semaphore, _semaphore_loop = asyncio.Semaphore(size), loop
    return _semaphore


def child_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if environ is None else environ)
    for key in SCRUBBED_ENV:
        env.pop(key, None)
    env[WORKER_ENV] = "1"
    return env


def command(binary: str, model: str, instructions: str, schema: dict) -> list[str]:
    return [
        binary, "-p",
        "--model", model,
        "--output-format", "json",
        "--json-schema", json.dumps(schema),
        "--system-prompt", instructions,
        "--tools", "",
        "--setting-sources", "",
        "--strict-mcp-config", "--mcp-config", _EMPTY_MCP,
        "--no-session-persistence",
    ]


def parse_result[M: BaseModel](
    stdout: str, returncode: int, stderr: str, output_type: type[M]
) -> M:
    detail = (_result_text(stdout) or stderr or stdout or "").strip()[-400:]
    if returncode != 0:
        error = ClaudeCodeLimited if _is_limit(detail) else ClaudeCodeError
        raise error(f"claude exited {returncode}: {detail}")
    try:
        data = json.loads(stdout)
    except ValueError as exc:
        raise ClaudeCodeError(f"claude output is not JSON: {detail}") from exc
    if not isinstance(data, dict):
        raise ClaudeCodeError(f"claude output is not an object: {detail}")
    if data.get("is_error") or data.get("subtype") not in (None, "success"):
        reason = str(data.get("result") or "")
        error = ClaudeCodeLimited if _is_limit(reason) else ClaudeCodeError
        raise error(
            f"claude reported an error ({data.get('subtype')}): "
            f"{str(data.get('result') or '')[:400]}"
        )
    structured = data.get("structured_output")
    if structured is None:
        raise ClaudeCodeError("claude returned no structured_output")
    try:
        return output_type.model_validate(structured)
    except ValidationError as exc:
        raise ClaudeCodeError(f"structured_output does not match the schema: {exc}") from exc


async def _reap(proc: asyncio.subprocess.Process) -> None:
    """Kill the child and wait for it, even while the caller is being
    cancelled (the wait is shielded; a second cancel still leaves it
    killed)."""
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.shield(proc.wait())
    except asyncio.CancelledError:
        pass


def _is_limit(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _LIMIT_MARKERS)


class ClaudeCodeRunner:
    """One isolated `claude -p` structured call per `run()`."""

    def __init__(
        self, model: str, *, binary: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_S, environ: dict[str, str] | None = None,
    ) -> None:
        self.model = model_name(model)
        self.binary = binary or os.environ.get("MIRAGEN_CLAUDE_BIN") or "claude"
        self.timeout = timeout
        self._environ = environ

    async def run[M: BaseModel](self, instructions: str, prompt: str, output_type: type[M]) -> M:
        binary = shutil.which(self.binary) or self.binary
        argv = command(binary, self.model, instructions, output_type.model_json_schema())
        async with _limit():
            with tempfile.TemporaryDirectory(prefix="miragen-cc-") as workdir:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *argv, cwd=workdir, env=child_env(self._environ),
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                except OSError as exc:
                    raise ClaudeCodeError(f"cannot start {self.binary!r}: {exc}") from exc
                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(prompt.encode()), timeout=self.timeout
                    )
                except TimeoutError as exc:
                    await _reap(proc)
                    raise ClaudeCodeError(f"claude timed out after {self.timeout:.0f}s") from exc
                except BaseException:
                    # Cancelled from outside (a caller's wait_for around the
                    # whole recall): the child must not outlive the call,
                    # hold its deleted cwd, or escape the concurrency cap.
                    await _reap(proc)
                    raise
        return parse_result(
            stdout.decode(errors="replace"),
            proc.returncode if proc.returncode is not None else -1,
            stderr.decode(errors="replace"), output_type,
        )
