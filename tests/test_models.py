import pytest
from pydantic import ValidationError

from miragen.models import (
    AgentProfile,
    AgentSpec,
    CronTrigger,
    HttpTrigger,
    ModelSettings,
    OnComplete,
)


def _spec(**kw):
    return {"model": "anthropic:claude-haiku-4-5", "instructions": "Test.", **kw}


def _profile(**kw):
    return {
        "name": "test",
        "mode": "autonomous",
        "triggers": [{"type": "cron", "schedule": "0 * * * *"}],
        "spec": _spec(),
        **kw,
    }


class TestCronTrigger:
    def test_minimal(self):
        t = CronTrigger(type="cron", schedule="0 * * * *")
        assert t.schedule == "0 * * * *"
        assert t.default_prompt is None

    def test_with_default_prompt(self):
        t = CronTrigger(type="cron", schedule="*/5 * * * *", default_prompt="Run report.")
        assert t.default_prompt == "Run report."


class TestCronValidation:
    @pytest.mark.parametrize("bad", ["0 9 * *", "61 * * * *", "not cron", ""])
    def test_invalid_expressions_raise(self, bad):
        with pytest.raises(ValidationError):
            CronTrigger(type="cron", schedule=bad)

    @pytest.mark.parametrize("good", ["0 * * * *", "*/5 * * * *", "0 9 * * 1-5"])
    def test_valid_expressions_ok(self, good):
        t = CronTrigger(type="cron", schedule=good)
        assert t.schedule == good

    def test_error_message_names_expression_and_example(self):
        with pytest.raises(ValidationError, match=r"0 9 \* \*") as exc_info:
            CronTrigger(type="cron", schedule="0 9 * *")
        assert "0 9 * * 1-5" in str(exc_info.value)


class TestHttpTrigger:
    def test_minimal(self):
        t = HttpTrigger(type="http")
        assert t.header_prompt is None

    def test_with_header_prompt(self):
        t = HttpTrigger(type="http", header_prompt="Be concise.")
        assert t.header_prompt == "Be concise."


class TestModelSettings:
    def test_all_optional(self):
        ms = ModelSettings()
        assert ms.max_tokens is None
        assert ms.temperature is None

    def test_max_tokens_only(self):
        ms = ModelSettings(max_tokens=512)
        assert ms.max_tokens == 512

    def test_full(self):
        ms = ModelSettings(max_tokens=1000, temperature=0.3)
        assert ms.temperature == 0.3


class TestOnComplete:
    def test_all_optional(self):
        oc = OnComplete()
        assert oc.log_to is None
        assert oc.notify is None
        assert oc.post_to is None

    def test_with_post_to(self):
        oc = OnComplete(post_to="https://example.com/hook")
        assert str(oc.post_to).startswith("https://example.com")

    def test_invalid_url_raises(self):
        with pytest.raises(ValidationError):
            OnComplete(post_to="not-a-url")

    def test_all_fields(self):
        oc = OnComplete(log_to="miradb", notify="telegram", post_to="https://example.com/")
        assert oc.log_to == "miradb"
        assert oc.notify == "telegram"


class TestAgentProfile:
    def test_valid_autonomous_cron(self):
        p = AgentProfile.model_validate(_profile())
        assert p.name == "test"
        assert p.mode == "autonomous"

    def test_valid_interactive_http(self):
        p = AgentProfile.model_validate(_profile(
            mode="interactive",
            triggers=[{"type": "http"}],
        ))
        assert p.mode == "interactive"

    def test_valid_hybrid_both_triggers(self):
        p = AgentProfile.model_validate(_profile(
            mode="hybrid",
            triggers=[{"type": "cron", "schedule": "0 * * * *"}, {"type": "http"}],
        ))
        assert len(p.triggers) == 2

    def test_autonomous_http_only_raises(self):
        with pytest.raises(ValidationError, match="cron trigger"):
            AgentProfile.model_validate(_profile(
                mode="autonomous",
                triggers=[{"type": "http"}],
            ))

    def test_interactive_with_cron_raises(self):
        with pytest.raises(ValidationError, match="cron triggers"):
            AgentProfile.model_validate(_profile(
                mode="interactive",
                triggers=[{"type": "cron", "schedule": "0 * * * *"}],
            ))

    def test_autonomous_with_both_triggers_ok(self):
        p = AgentProfile.model_validate(_profile(
            mode="autonomous",
            triggers=[{"type": "cron", "schedule": "0 * * * *"}, {"type": "http"}],
        ))
        assert p.mode == "autonomous"

    def test_tools_list(self):
        p = AgentProfile.model_validate(_profile(tools=["tool_a", "tool_b"]))
        assert p.tools == ["tool_a", "tool_b"]

    def test_tools_none_by_default(self):
        p = AgentProfile.model_validate(_profile())
        assert p.tools is None

    def test_model_settings(self):
        p = AgentProfile.model_validate(_profile(
            spec=_spec(model_settings={"max_tokens": 256, "temperature": 0.5}),
        ))
        assert p.spec.model_settings.max_tokens == 256
        assert p.spec.model_settings.temperature == 0.5

    def test_capabilities_list(self):
        p = AgentProfile.model_validate(_profile(
            spec=_spec(capabilities=["WebSearch"]),
        ))
        assert p.spec.capabilities == ["WebSearch"]

    def test_max_steps(self):
        p = AgentProfile.model_validate(_profile(spec=_spec(max_steps=10)))
        assert p.spec.max_steps == 10

    def test_on_complete(self):
        p = AgentProfile.model_validate(_profile(
            on_complete={"log_to": "miradb", "notify": "telegram"},
        ))
        assert p.on_complete.log_to == "miradb"

    def test_approval_required(self):
        p = AgentProfile.model_validate(_profile(approval_required=["delete_*", "execute_*"]))
        assert "delete_*" in p.approval_required


class TestStrictSchema:
    def test_unknown_top_level_key_raises(self):
        with pytest.raises(ValidationError, match="aproval_required"):
            AgentProfile.model_validate(_profile(aproval_required=["delete_*"]))

    def test_unknown_spec_key_raises(self):
        with pytest.raises(ValidationError, match="instrutions"):
            AgentProfile.model_validate(_profile(spec=_spec(instrutions="typo")))

    def test_unknown_trigger_key_raises(self):
        with pytest.raises(ValidationError):
            AgentProfile.model_validate(_profile(
                triggers=[{"type": "cron", "schedule": "0 * * * *", "prompt": "wrong key"}],
            ))

    def test_unknown_model_settings_key_raises(self):
        with pytest.raises(ValidationError, match="temprature"):
            AgentProfile.model_validate(_profile(
                spec=_spec(model_settings={"temprature": 0.5}),
            ))

    def test_empty_triggers_raises(self):
        with pytest.raises(ValidationError):
            AgentProfile.model_validate(_profile(triggers=[]))

    @pytest.mark.parametrize("bad", ["Upper", "has space", "-leading", "a/b", ""])
    def test_invalid_name_raises(self, bad):
        with pytest.raises(ValidationError):
            AgentProfile.model_validate(_profile(name=bad))

    @pytest.mark.parametrize("good", ["a", "morning-briefing", "agent_2", "0abc"])
    def test_valid_name_ok(self, good):
        assert AgentProfile.model_validate(_profile(name=good)).name == good

    def test_temperature_out_of_range_raises(self):
        with pytest.raises(ValidationError):
            AgentProfile.model_validate(_profile(
                spec=_spec(model_settings={"temperature": 3.0}),
            ))

    def test_max_steps_zero_raises(self):
        with pytest.raises(ValidationError):
            AgentProfile.model_validate(_profile(spec=_spec(max_steps=0)))

    def test_approval_wire_models_stay_lenient(self):
        # Third-party approval services may attach extra metadata — must not raise.
        from miragen.models import ApprovalRequest, ApprovalResponse
        req = ApprovalRequest.model_validate({
            "agent_name": "a", "tool_name": "t", "tool_args": {}, "request_id": "id",
            "extra_meta": "ok",
        })
        assert req.agent_name == "a"
        resp = ApprovalResponse.model_validate({"approved": True, "reviewer": "human"})
        assert resp.approved is True
