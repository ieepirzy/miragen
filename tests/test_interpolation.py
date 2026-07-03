import yaml

import pytest

from miragen.load import interpolate_env, load_profile


class TestInterpolateEnv:
    def test_substitutes_string_value(self, monkeypatch):
        monkeypatch.setenv("FOO", "bar")
        assert interpolate_env("${FOO}") == "bar"

    def test_substitutes_inside_larger_string(self, monkeypatch):
        monkeypatch.setenv("HOST", "example.com")
        assert interpolate_env("https://${HOST}/webhook") == "https://example.com/webhook"

    def test_substitutes_in_block_scalar_style_string(self, monkeypatch):
        monkeypatch.setenv("NAME", "Ilari")
        text = "You are an assistant for ${NAME}.\nBe concise."
        assert interpolate_env(text) == "You are an assistant for Ilari.\nBe concise."

    def test_recurses_into_nested_dicts_and_lists(self, monkeypatch):
        monkeypatch.setenv("URL", "http://mcp:9000")
        doc = {
            "spec": {
                "capabilities": [
                    "WebSearch",
                    {"MCP": {"url": "${URL}"}},
                ]
            }
        }
        result = interpolate_env(doc)
        assert result["spec"]["capabilities"][1]["MCP"]["url"] == "http://mcp:9000"

    def test_dict_keys_never_interpolated(self, monkeypatch):
        monkeypatch.setenv("KEY", "renamed")
        doc = {"${KEY}": "value"}
        result = interpolate_env(doc)
        assert "${KEY}" in result
        assert result["${KEY}"] == "value"

    def test_non_string_scalars_pass_through(self):
        doc = {"max_steps": 10, "enabled": True, "ratio": 0.5, "nothing": None}
        assert interpolate_env(doc) == doc

    def test_default_used_when_unset(self, monkeypatch):
        monkeypatch.delenv("MISSING_VAR", raising=False)
        assert interpolate_env("${MISSING_VAR:-fallback}") == "fallback"

    def test_env_value_used_over_default(self, monkeypatch):
        monkeypatch.setenv("SET_VAR", "actual")
        assert interpolate_env("${SET_VAR:-fallback}") == "actual"

    def test_empty_string_env_value_counts_as_set(self, monkeypatch):
        monkeypatch.setenv("EMPTY_VAR", "")
        assert interpolate_env("${EMPTY_VAR:-fallback}") == ""

    def test_undefined_without_default_raises(self, monkeypatch):
        monkeypatch.delenv("NOPE", raising=False)
        with pytest.raises(ValueError, match="NOPE"):
            interpolate_env("${NOPE}")

    def test_error_includes_yaml_path(self, monkeypatch):
        monkeypatch.delenv("FOO", raising=False)
        doc = {"spec": {"capabilities": [{}, {}, {"MCP": {"url": "${FOO}"}}]}}
        with pytest.raises(ValueError, match=r"spec\.capabilities\[2\]\.MCP\.url"):
            interpolate_env(doc)

    def test_escape_produces_literal(self, monkeypatch):
        monkeypatch.setenv("FOO", "bar")
        assert interpolate_env("$${FOO}") == "${FOO}"

    def test_escape_does_not_consume_env(self, monkeypatch):
        monkeypatch.delenv("UNSET", raising=False)
        # Escaped form must not raise even though UNSET has no default.
        assert interpolate_env("$${UNSET}") == "${UNSET}"


class TestLoadProfileInterpolation:
    def _valid_data(self, **kw):
        return {
            "name": "test",
            "mode": "autonomous",
            "triggers": [{"type": "cron", "schedule": "0 * * * *"}],
            "spec": {"model": "anthropic:claude-haiku-4-5", "instructions": "Test."},
            **kw,
        }

    def test_interpolates_before_validation(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MCP_URL", "http://localhost:9001")
        data = self._valid_data()
        data["spec"]["capabilities"] = [{"MCP": {"url": "${MCP_URL}"}}]
        path = tmp_path / "agent.yaml"
        path.write_text(yaml.dump(data))

        profile = load_profile(path)
        assert profile.spec.capabilities[0]["MCP"]["url"] == "http://localhost:9001"

    def test_undefined_var_fails_validate(self, tmp_path, monkeypatch):
        monkeypatch.delenv("UNDEFINED_WEBHOOK", raising=False)
        data = self._valid_data(spec={
            "model": "anthropic:claude-haiku-4-5",
            "instructions": "Reach ${UNDEFINED_WEBHOOK}.",
        })
        path = tmp_path / "agent.yaml"
        path.write_text(yaml.dump(data))

        with pytest.raises(ValueError, match="UNDEFINED_WEBHOOK"):
            load_profile(path)
