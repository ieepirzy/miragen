"""Duck-typed stand-ins for a PydanticAI agent + run result, shaped exactly
as miragen.runs.extract_run_details walks them (parts with part_kind
tool-call / tool-return / retry-prompt, usage with requests/token attrs).

Named test_model_tier_stub so pytest collects nothing from it directly; it
exists to be imported by the tier-parity and OTel test modules.
"""

from types import SimpleNamespace


class _Part(SimpleNamespace):
    pass


def _messages():
    return [
        SimpleNamespace(kind="request", parts=[
            _Part(part_kind="user-prompt", content="go"),
        ]),
        SimpleNamespace(kind="response", parts=[
            _Part(part_kind="tool-call", tool_call_id="c1", tool_name="search",
                  args='{"q": "x"}'),
            _Part(part_kind="tool-call", tool_call_id="c2", tool_name="write_file",
                  args='{"path": "a"}'),
        ]),
        SimpleNamespace(kind="request", parts=[
            _Part(part_kind="tool-return", tool_call_id="c1", outcome="success",
                  content="found"),
            _Part(part_kind="retry-prompt", tool_call_id="c2", content="denied"),
        ]),
        SimpleNamespace(kind="response", parts=[
            _Part(part_kind="text", content="all done"),
        ]),
    ]


class StubResult:
    def __init__(self):
        self.output = "all done"
        self.usage = SimpleNamespace(requests=2, input_tokens=120, output_tokens=30)

    def all_messages(self):
        return _messages()


class StubAgent:
    """Stands in for app_module._agent: run() records prompts and returns a
    StubResult (or raises)."""

    def __init__(self, raise_exc: Exception | None = None):
        self._raise = raise_exc
        self.prompts: list[str] = []

    async def run(self, prompt, usage_limits=None, message_history=None):
        self.prompts.append(prompt)
        if self._raise is not None:
            raise self._raise
        return StubResult()


def model_profile(**kw):
    from miragen.models import AgentProfile

    return AgentProfile.model_validate({
        "name": "model-worker",
        "mode": "interactive",
        "triggers": [{"type": "http"}],
        "spec": {"model": "anthropic:claude-haiku-4-5", "instructions": "help"},
        **kw,
    })
