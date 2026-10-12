"""`miragen validate` as a CI gate (#143): the runtime's own loader, any
number of profiles, nonzero on any invalid one, no secrets in the output."""

from __future__ import annotations

import yaml
from click.testing import CliRunner

from miragen.cli import cli


def _profile(**over) -> dict:
    doc = {
        "name": "seo-audit", "mode": "interactive", "triggers": [{"type": "http"}],
        "spec": {"model": "anthropic:claude-haiku-4-5", "instructions": "Audit."},
    }
    doc.update(over)
    return doc


def _write(tmp_path, name, doc) -> str:
    path = tmp_path / name
    path.write_text(doc if isinstance(doc, str) else yaml.safe_dump(doc))
    return str(path)


def _validate(tmp_path, monkeypatch, *args, env=None):
    monkeypatch.chdir(tmp_path)  # no tools.py here
    return CliRunner().invoke(cli, ["validate", *args], env=env or {}, catch_exceptions=False)


def test_a_valid_profile_exits_zero(tmp_path, monkeypatch):
    result = _validate(tmp_path, monkeypatch, _write(tmp_path, "a.yaml", _profile()))
    assert result.exit_code == 0, result.output
    assert "✓ 'seo-audit' is valid" in result.output


def test_a_missing_spec_exits_nonzero_and_names_it(tmp_path, monkeypatch):
    doc = _profile()
    del doc["spec"]
    result = _validate(tmp_path, monkeypatch, _write(tmp_path, "a.yaml", doc))
    assert result.exit_code == 1
    assert "✗ Invalid profile" in result.output and "spec" in result.output


def test_malformed_yaml_and_a_missing_file_exit_nonzero(tmp_path, monkeypatch):
    bad = _write(tmp_path, "bad.yaml", "name: [unclosed\n")
    assert _validate(tmp_path, monkeypatch, bad).exit_code == 1
    missing = _validate(tmp_path, monkeypatch, str(tmp_path / "nope.yaml"))
    assert missing.exit_code == 1 and "nope.yaml" in missing.output


def test_every_profile_is_checked_and_any_failure_fails_the_run(tmp_path, monkeypatch):
    good = _write(tmp_path, "good.yaml", _profile())
    bad_doc = _profile(name="broken")
    del bad_doc["spec"]
    bad = _write(tmp_path, "bad.yaml", bad_doc)
    other = _write(tmp_path, "other.yaml", _profile(name="other"))
    result = _validate(tmp_path, monkeypatch, good, bad, other)
    assert result.exit_code == 1
    # The bad one in the middle doesn't stop the ones after it.
    assert "'seo-audit' is valid" in result.output and "'other' is valid" in result.output
    assert f"{bad}:" in result.output
    assert "2/3 profile(s) valid" in result.output


def test_agent_profile_is_the_default(tmp_path, monkeypatch):
    path = _write(tmp_path, "agent.yaml", _profile())
    result = _validate(tmp_path, monkeypatch, env={"AGENT_PROFILE": path})
    assert result.exit_code == 0, result.output


def test_interpolated_capability_config_is_not_printed(tmp_path, monkeypatch):
    doc = _profile()
    doc["spec"]["capabilities"] = ["WebSearch", {"MCP": {
        "url": "https://mcp.example.com/mcp?key=${MCP_KEY}", "name": "home"}}]
    result = _validate(tmp_path, monkeypatch, _write(tmp_path, "a.yaml", doc),
                       env={"MCP_KEY": "sk-very-secret"})
    assert result.exit_code == 0, result.output
    assert "sk-very-secret" not in result.output
    assert "['WebSearch', 'MCP(home)']" in result.output
