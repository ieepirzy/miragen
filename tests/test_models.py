import pytest
from pydantic import ValidationError

from miragen.models import (
    AgentProfile,
    AgentSpec,
    CronTrigger,
    HttpTrigger,
    IntervalTrigger,
    Limits,
    ModelSettings,
    OnComplete,
    StartupTrigger,
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


class TestIntervalTrigger:
    def test_minimal(self):
        t = IntervalTrigger(type="interval", every_s=900)
        assert t.every_s == 900
        assert t.default_prompt is None

    def test_with_default_prompt(self):
        t = IntervalTrigger(type="interval", every_s=60, default_prompt="Poll the feed.")
        assert t.default_prompt == "Poll the feed."

    def test_every_s_minimum(self):
        with pytest.raises(ValidationError):
            IntervalTrigger(type="interval", every_s=5)

    def test_every_s_boundary_ok(self):
        t = IntervalTrigger(type="interval", every_s=10)
        assert t.every_s == 10

    def test_every_s_non_int_raises(self):
        with pytest.raises(ValidationError):
            IntervalTrigger(type="interval", every_s="fast")

    def test_unknown_key_raises(self):
        with pytest.raises(ValidationError):
            IntervalTrigger(type="interval", every_s=60, prompt="wrong key")


class TestStartupTrigger:
    def test_minimal(self):
        t = StartupTrigger(type="startup")
        assert t.default_prompt is None
        assert t.delay_s == 0

    def test_with_delay(self):
        t = StartupTrigger(type="startup", delay_s=5, default_prompt="Announce you are online.")
        assert t.delay_s == 5
        assert t.default_prompt == "Announce you are online."

    def test_negative_delay_raises(self):
        with pytest.raises(ValidationError):
            StartupTrigger(type="startup", delay_s=-1)


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

    def test_autonomous_interval_and_http_ok(self):
        p = AgentProfile.model_validate(_profile(
            mode="autonomous",
            triggers=[{"type": "interval", "every_s": 60}, {"type": "http"}],
        ))
        assert p.mode == "autonomous"

    def test_autonomous_interval_only_ok(self):
        p = AgentProfile.model_validate(_profile(
            mode="autonomous",
            triggers=[{"type": "interval", "every_s": 60}],
        ))
        assert p.mode == "autonomous"

    def test_interactive_with_interval_raises(self):
        with pytest.raises(ValidationError, match="cron triggers"):
            AgentProfile.model_validate(_profile(
                mode="interactive",
                triggers=[{"type": "interval", "every_s": 60}],
            ))

    def test_interactive_with_startup_ok(self):
        p = AgentProfile.model_validate(_profile(
            mode="interactive",
            triggers=[{"type": "http"}, {"type": "startup"}],
        ))
        assert p.mode == "interactive"

    def test_autonomous_with_startup_only_raises(self):
        # startup doesn't count as self-activating for the http-only check.
        with pytest.raises(ValidationError, match="cron trigger"):
            AgentProfile.model_validate(_profile(
                mode="autonomous",
                triggers=[{"type": "http"}, {"type": "startup"}],
            ))

    def test_hybrid_with_startup_and_cron_ok(self):
        p = AgentProfile.model_validate(_profile(
            mode="hybrid",
            triggers=[
                {"type": "cron", "schedule": "0 * * * *"},
                {"type": "http"},
                {"type": "startup"},
            ],
        ))
        assert len(p.triggers) == 3

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


class TestApprovalMode:
    def test_defaults(self):
        p = AgentProfile.model_validate(_profile())
        assert p.approval_mode == "open"
        assert p.approval_timeout_s == 300

    def test_strict_with_approval_required_ok(self):
        p = AgentProfile.model_validate(_profile(
            approval_required=["delete_*"], approval_mode="strict",
        ))
        assert p.approval_mode == "strict"

    def test_queue_with_approval_required_ok(self):
        p = AgentProfile.model_validate(_profile(
            approval_required=["delete_*"], approval_mode="queue", approval_timeout_s=60,
        ))
        assert p.approval_mode == "queue"
        assert p.approval_timeout_s == 60

    def test_strict_without_approval_required_raises(self):
        with pytest.raises(ValidationError, match="approval_required"):
            AgentProfile.model_validate(_profile(approval_mode="strict"))

    def test_queue_without_approval_required_raises(self):
        with pytest.raises(ValidationError, match="approval_required"):
            AgentProfile.model_validate(_profile(approval_mode="queue"))

    def test_nondefault_timeout_without_approval_required_raises(self):
        with pytest.raises(ValidationError, match="approval_required"):
            AgentProfile.model_validate(_profile(approval_timeout_s=60))

    def test_default_mode_and_timeout_without_approval_required_ok(self):
        # approval_required unset, but approval_mode/timeout are both left at
        # their defaults — nothing to flag as dead config.
        p = AgentProfile.model_validate(_profile())
        assert p.approval_required is None

    def test_invalid_mode_raises(self):
        with pytest.raises(ValidationError):
            AgentProfile.model_validate(_profile(
                approval_required=["delete_*"], approval_mode="lenient",
            ))

    def test_timeout_zero_raises(self):
        with pytest.raises(ValidationError):
            AgentProfile.model_validate(_profile(
                approval_required=["delete_*"], approval_timeout_s=0,
            ))


class TestLimits:
    def test_tokens_per_run_only(self):
        limits = Limits(tokens_per_run=200_000)
        assert limits.tokens_per_run == 200_000
        assert limits.tokens_per_day is None
        assert limits.on_exceeded == "skip"

    def test_tokens_per_day_only(self):
        limits = Limits(tokens_per_day=2_000_000)
        assert limits.tokens_per_day == 2_000_000

    def test_both_caps(self):
        limits = Limits(tokens_per_run=200_000, tokens_per_day=2_000_000, on_exceeded="notify")
        assert limits.on_exceeded == "notify"

    def test_empty_block_raises(self):
        with pytest.raises(ValidationError, match="at least one"):
            Limits()

    def test_zero_tokens_per_run_raises(self):
        with pytest.raises(ValidationError):
            Limits(tokens_per_run=0)

    def test_zero_tokens_per_day_raises(self):
        with pytest.raises(ValidationError):
            Limits(tokens_per_day=0)

    def test_invalid_on_exceeded_raises(self):
        with pytest.raises(ValidationError):
            Limits(tokens_per_run=1, on_exceeded="explode")

    def test_unknown_key_raises(self):
        with pytest.raises(ValidationError):
            Limits.model_validate({"tokens_per_run": 1, "cost_usd": 5})


class TestAgentProfileLimits:
    def test_profile_without_limits_unchanged(self):
        p = AgentProfile.model_validate(_profile())
        assert p.limits is None

    def test_profile_with_limits(self):
        p = AgentProfile.model_validate(_profile(
            limits={"tokens_per_run": 200_000, "tokens_per_day": 2_000_000},
        ))
        assert p.limits.tokens_per_run == 200_000
        assert p.limits.tokens_per_day == 2_000_000

    def test_empty_limits_block_raises(self):
        with pytest.raises(ValidationError, match="at least one"):
            AgentProfile.model_validate(_profile(limits={}))


class TestAgentProfileHistoryMaxMessages:
    def test_defaults_to_unbounded(self):
        p = AgentProfile.model_validate(_profile())
        assert p.history_max_messages is None

    def test_accepts_positive_int(self):
        p = AgentProfile.model_validate(_profile(history_max_messages=50))
        assert p.history_max_messages == 50

    def test_zero_raises(self):
        with pytest.raises(ValidationError):
            AgentProfile.model_validate(_profile(history_max_messages=0))

    def test_negative_raises(self):
        with pytest.raises(ValidationError):
            AgentProfile.model_validate(_profile(history_max_messages=-1))
