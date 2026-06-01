import pytest
import yaml
from unittest.mock import patch

from miragen.load import resolve_capabilities, load_profile, _CAPABILITY_REGISTRY, register_capability


class TestResolveCapabilities:
    def test_empty_list(self):
        assert resolve_capabilities([]) == []

    def test_string_form(self):
        caps = resolve_capabilities(["WebSearch"])
        assert len(caps) == 1

    def test_dict_form_with_config(self):
        caps = resolve_capabilities([{"Thinking": {"effort": "high"}}])
        assert len(caps) == 1

    def test_dict_form_null_config(self):
        caps = resolve_capabilities([{"Thinking": None}])
        assert len(caps) == 1

    def test_mcp_with_url(self):
        caps = resolve_capabilities([{"MCP": {"url": "http://localhost:8080", "name": "test"}}])
        assert len(caps) == 1

    def test_mcp_url_required(self):
        with pytest.raises(KeyError):
            resolve_capabilities([{"MCP": {}}])

    def test_image_generation_default(self):
        caps = resolve_capabilities(["ImageGeneration"])
        assert len(caps) == 1

    def test_image_generation_custom_model(self):
        caps = resolve_capabilities([{"ImageGeneration": {"fallback_model": "openai-responses:gpt-4o-mini"}}])
        assert len(caps) == 1

    def test_unknown_capability_raises(self):
        with pytest.raises(ValueError, match="register_capability"):
            resolve_capabilities(["NoSuchCap"])

    def test_multi_key_dict_raises(self):
        with pytest.raises(ValueError, match="exactly one key"):
            resolve_capabilities([{"WebSearch": {}, "Thinking": {}}])

    def test_unexpected_type_raises(self):
        with pytest.raises(ValueError, match="Unexpected capability format"):
            resolve_capabilities([42])  # type: ignore

    def test_multiple_capabilities(self):
        caps = resolve_capabilities(["WebSearch", {"Thinking": {"effort": "low"}}, "WebFetch"])
        assert len(caps) == 3

    def test_all_builtin_names_present(self):
        assert set(_CAPABILITY_REGISTRY.keys()) == {"WebSearch", "WebFetch", "Thinking", "ImageGeneration", "MCP"}


class TestRegisterCapability:
    def test_registers_and_resolves(self):
        class FakeCap:
            def __init__(self, size: int):
                self.size = size

        with patch.dict("miragen.load._CAPABILITY_REGISTRY", {}, clear=False):
            @register_capability("FakeCap")
            def _(cfg):
                return FakeCap(cfg.get("size", 10))

            caps = resolve_capabilities([{"FakeCap": {"size": 42}}])
            assert len(caps) == 1
            assert caps[0].size == 42

    def test_returns_factory_unchanged(self):
        sentinel = lambda cfg: None
        with patch.dict("miragen.load._CAPABILITY_REGISTRY", {}):
            result = register_capability("SentinelCap")(sentinel)
        assert result is sentinel

    def test_overwrites_existing(self):
        calls = []
        with patch.dict("miragen.load._CAPABILITY_REGISTRY", {}):
            @register_capability("DupCap")
            def first(cfg):
                return "first"

            @register_capability("DupCap")
            def second(cfg):
                calls.append("second")
                return "second"

            resolve_capabilities(["DupCap"])
        assert calls == ["second"]


class TestLoadProfile:
    def _valid_data(self, **kw):
        return {
            "name": "test",
            "mode": "autonomous",
            "triggers": [{"type": "cron", "schedule": "0 * * * *"}],
            "spec": {"model": "anthropic:claude-haiku-4-5", "instructions": "Test."},
            **kw,
        }

    def _write(self, tmp_path, data):
        p = tmp_path / "agent.yaml"
        p.write_text(yaml.dump(data))
        return p

    def test_loads_valid_profile(self, tmp_path):
        path = self._write(tmp_path, self._valid_data())
        p = load_profile(path)
        assert p.name == "test"
        assert p.mode == "autonomous"

    def test_accepts_string_path(self, tmp_path):
        path = self._write(tmp_path, self._valid_data())
        p = load_profile(str(path))
        assert p.name == "test"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="not found"):
            load_profile(tmp_path / "missing.yaml")

    def test_non_dict_yaml_raises(self, tmp_path):
        p = tmp_path / "bad.yaml"
        p.write_text("- just a list\n")
        with pytest.raises(ValueError, match="YAML mapping"):
            load_profile(p)

    def test_unknown_capability_raises_at_load(self, tmp_path):
        data = self._valid_data()
        data["spec"]["capabilities"] = ["NoSuchCap"]
        path = self._write(tmp_path, data)
        with pytest.raises(ValueError, match="register_capability"):
            load_profile(path)

    def test_valid_capabilities_resolve_at_load(self, tmp_path):
        data = self._valid_data()
        data["spec"]["capabilities"] = ["WebSearch"]
        path = self._write(tmp_path, data)
        p = load_profile(path)
        assert p.spec.capabilities == ["WebSearch"]

    def test_model_settings_parsed(self, tmp_path):
        data = self._valid_data()
        data["spec"]["model_settings"] = {"max_tokens": 100, "temperature": 0.5}
        path = self._write(tmp_path, data)
        p = load_profile(path)
        assert p.spec.model_settings.max_tokens == 100

    def test_on_complete_parsed(self, tmp_path):
        data = self._valid_data()
        data["on_complete"] = {"log_to": "miradb"}
        path = self._write(tmp_path, data)
        p = load_profile(path)
        assert p.on_complete.log_to == "miradb"

    def test_full_example_yaml(self, tmp_path):
        import os
        example = (
            (tmp_path.parent.parent / "example.yaml")
            if (tmp_path.parent.parent / "example.yaml").exists()
            else None
        )
        if example is None:
            pytest.skip("example.yaml not found")
        p = load_profile(example)
        assert p.name == "morning-briefing"
