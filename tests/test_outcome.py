"""Backend-owned whole-run outcome classification from harvested diffs."""

from miragen.outcome import (
    classify_categories,
    classify_harvested_diff,
    classify_path,
    extract_changed_paths,
)


def test_extract_changed_paths_from_unified_diff() -> None:
    diff = """\
diff --git a/src/app.py b/src/app.py
index 111..222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1 @@
-old
+new
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-# old
+# new
"""
    assert extract_changed_paths(diff) == ["src/app.py", "README.md"]


def test_classify_path_categories() -> None:
    assert classify_path("src/main.py") == "code"
    assert classify_path("README.md") == "documentation"
    assert classify_path("docs/guide.md") == "documentation"
    assert classify_path(".github/workflows/ci.yml") == "structural"
    assert classify_path("pyproject.toml") == "structural"
    assert classify_path("Dockerfile") == "structural"
    assert classify_path("infra/main.tf") == "structural"


def test_classify_categories_are_sorted_and_unique() -> None:
    assert classify_categories(
        ["src/a.py", "README.md", ".github/workflows/ci.yml", "src/b.py"]
    ) == ["documentation", "code", "structural"]


def test_classic_workspace_has_no_named_repositories() -> None:
    diff = """\
diff --git a/hello.py b/hello.py
--- a/hello.py
+++ b/hello.py
@@ -0,0 +1 @@
+print("hi")
"""
    affected, categories = classify_harvested_diff(diff)
    assert affected == []
    assert categories == ["code"]


def test_multi_repo_affected_only_nonempty_patches() -> None:
    app_patch = """\
diff --git a/main.py b/main.py
--- a/main.py
+++ b/main.py
@@ -1 +1 @@
-a
+b
"""
    docs_patch = """\
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-old
+new
"""
    empty_patch = ""
    bundle = "\n".join(
        [
            "# === miragen repository: app (mount: app) ===",
            app_patch,
            "# === miragen repository: docs (mount: docs) ===",
            docs_patch,
            "# === miragen repository: tools (mount: tools) ===",
            empty_patch,
        ]
    )
    affected, categories = classify_harvested_diff(
        bundle,
        patches_by_name={
            "app": app_patch,
            "docs": docs_patch,
            "tools": empty_patch,
        },
    )
    assert affected == ["app", "docs"]
    assert categories == ["documentation", "code"]


def test_empty_harvest_is_known_empty_not_unknown() -> None:
    affected, categories = classify_harvested_diff("", patches_by_name={"app": ""})
    assert affected == []
    assert categories == []
