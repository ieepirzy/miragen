"""The pr-scope classifier (scripts/ci/pr_scope.py): deploy config and app
code never in one PR, unless a person adds the `mixed-change-ok` label."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "pr_scope.py"
spec = importlib.util.spec_from_file_location("pr_scope", SCRIPT)
pr_scope = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pr_scope)

CONFIG = [
    "compose*.y*ml", ".env*", "Dockerfile*", "deploy/", "k8s/", "infra/", ".github/workflows/",
]
SOURCE = ["src/", "app"]


def run(paths, override=False):
    return pr_scope.verdict(*pr_scope.classify(paths, CONFIG, SOURCE), override)[0]


def test_config_patterns():
    for path in ("compose.yaml", "compose.ci.yml", "deploy/compose.prod.yml", ".env",
                 ".env.example", "Dockerfile", "Dockerfile.miragend", "deploy/stack.md",
                 "k8s/app.yaml", "infra/x.tf", "sub/infra/x.tf", ".github/workflows/ci.yml"):
        assert pr_scope.is_config(path, CONFIG), path
    for path in ("src/app/compose_helper.py", "docs/deploy.md", "README.md", "src/environment.py"):
        assert not pr_scope.is_config(path, CONFIG), path


def test_source_excludes_tests():
    assert pr_scope.is_source("src/app/x.py", SOURCE)
    assert pr_scope.is_source("app/main.py", SOURCE)
    for path in ("src/app/tests/test_x.py", "tests/test_x.py", "src/app/conftest.py",
                 "src/web/x.test.ts", "docs/x.md", "application/x.py"):
        assert not pr_scope.is_source(path, SOURCE), path


def test_mixed_fails_and_the_label_allows_it():
    mixed = ["compose.yaml", "src/app/x.py"]
    assert run(mixed) == 1
    assert run(mixed, override=True) == 0


def test_either_alone_or_with_tests_and_docs_passes():
    assert run(["compose.yaml", ".env.example", "docs/deploy.md"]) == 0
    assert run(["src/app/x.py", "tests/test_x.py", "README.md"]) == 0
    assert run(["compose.yaml", "tests/test_compose.py"]) == 0
    assert run([]) == 0


def test_a_dockerfile_under_src_is_config():
    assert run(["src/Dockerfile"]) == 0
    assert run(["src/Dockerfile", "src/app/x.py"]) == 1


def test_a_rename_counts_its_old_path(monkeypatch):
    """Moving compose.yaml into src/app/ while editing code still mixes."""
    import io
    import json

    rows = [{"filename": "src/app/compose_moved.txt", "previous_filename": "compose.yaml"},
            {"filename": "src/app/x.py"}]
    monkeypatch.setattr(
        pr_scope.urllib.request, "urlopen",
        lambda request, timeout: io.BytesIO(json.dumps(rows).encode()),
    )
    paths = pr_scope.fetch_files("https://api.test", "o/r", 7, "t")
    assert "compose.yaml" in paths
    assert run(paths) == 1


def test_an_unreadable_file_list_fails_closed(monkeypatch, tmp_path):
    for name in ("GITHUB_REPOSITORY", "PR_NUMBER", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert pr_scope.main(["--config", "compose*.y*ml", "--source", "src/"]) == 1


def test_the_label_is_read_from_the_environment(monkeypatch, tmp_path):
    files = tmp_path / "files.txt"
    files.write_text("compose.yaml\nsrc/app/x.py\n")
    argv = ["--files", str(files)]
    for pattern in CONFIG:
        argv += ["--config", pattern]
    for prefix in SOURCE:
        argv += ["--source", prefix]
    monkeypatch.delenv("PR_SCOPE_OVERRIDE", raising=False)
    assert pr_scope.main(argv) == 1
    monkeypatch.setenv("PR_SCOPE_OVERRIDE", "true")
    assert pr_scope.main(argv) == 0
