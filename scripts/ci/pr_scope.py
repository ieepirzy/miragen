#!/usr/bin/env python3
"""pr-scope: a pull request changes deploy config OR app code, never both.

Why: the release controller's automatic rollback reverts only the app image.
A redeploy re-reads compose and env from main, so a PR that changes both
cannot be undone by rolling back. Split it: config in its own small PR.

Deploy config (`--config`): a pattern ending in `/` is a directory, matched
at the repository root or under any directory (`deploy/`, `infra/`,
`.github/workflows/`); any other pattern is matched against the file's base
name (`compose*.y*ml`, `.env*`, `Dockerfile*`). App code (`--source`): paths
under the given prefixes, test files excluded. A rename counts both its old
and its new path.

Escape hatch: the `mixed-change-ok` label (PR_SCOPE_OVERRIDE=true).

Stdlib only (the CI runner image has python3 but no pip, and no gh). It
fails closed: if the file list cannot be read, the check fails.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Iterable

LABEL = "mixed-change-ok"
MAX_FILES = 3000  # GitHub lists at most this many; that many may be cut off

_TEST_DIRS = {"tests", "test", "__tests__", "testdata", "fixtures"}
_TEST_NAMES = ("test_*.py", "*_test.py", "conftest.py", "*.test.*", "*.spec.*")


def is_config(path: str, patterns: Iterable[str]) -> bool:
    name = path.rsplit("/", 1)[-1]
    for pattern in patterns:
        if pattern.endswith("/"):
            if path.startswith(pattern) or f"/{pattern}" in f"/{path}":
                return True
        elif fnmatch.fnmatchcase(name, pattern):
            return True
    return False


def is_test(path: str) -> bool:
    parts = path.split("/")
    if any(part in _TEST_DIRS for part in parts[:-1]):
        return True
    return any(fnmatch.fnmatchcase(parts[-1], pattern) for pattern in _TEST_NAMES)


def is_source(path: str, prefixes: Iterable[str]) -> bool:
    return not is_test(path) and any(
        path.startswith(prefix if prefix.endswith("/") else prefix + "/") for prefix in prefixes
    )


def classify(
    paths: Iterable[str], config: Iterable[str], source: Iterable[str]
) -> tuple[list[str], list[str]]:
    config, source = list(config), list(source)
    seen = sorted(set(paths))
    # A config file is never also counted as code (a Dockerfile under src/).
    found_config = [p for p in seen if is_config(p, config)]
    found_source = [p for p in seen if p not in found_config and is_source(p, source)]
    return found_config, found_source


def verdict(config_files: list[str], source_files: list[str], override: bool) -> tuple[int, str]:
    if not (config_files and source_files):
        return 0, "ok: this PR does not mix deploy config with app code"
    listing = (
        "deploy config:\n  " + "\n  ".join(config_files[:20])
        + "\napp code:\n  " + "\n  ".join(source_files[:20])
    )
    if override:
        return 0, (
            f"allowed by the `{LABEL}` label: this PR mixes deploy config with app code\n{listing}"
        )
    return 1, (
        "this PR mixes deploy config with app code. Rollback reverts only the app image and a "
        "redeploy re-reads compose/env from main, so a mixed change cannot be rolled back: put the "
        f"config in its own PR (or, if it truly cannot be split, a person adds the `{LABEL}` "
        f"label).\n{listing}"
    )


def fetch_files(api: str, repository: str, number: int, token: str) -> list[str]:
    paths: list[str] = []
    page, seen = 1, 0
    while True:
        request = urllib.request.Request(
            f"{api}/repos/{repository}/pulls/{number}/files?per_page=100&page={page}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            rows = json.load(response)
        if not isinstance(rows, list):
            raise TypeError("GitHub did not answer a list of files")
        for row in rows:
            paths.append(str(row["filename"]))
            if row.get("previous_filename"):
                paths.append(str(row["previous_filename"]))
        seen += len(rows)
        if len(rows) < 100:
            break
        page += 1
        if page > MAX_FILES // 100 + 1:
            break
    if seen >= MAX_FILES:
        raise ValueError(f"the PR changes {seen}+ files; GitHub's list may be cut off")
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", action="append", required=True, help="deploy-config pattern")
    parser.add_argument("--source", action="append", required=True, help="app-code path prefix")
    parser.add_argument("--files", help="read changed paths from this file instead of GitHub")
    args = parser.parse_args(argv)
    override = os.environ.get("PR_SCOPE_OVERRIDE", "").strip().lower() == "true"
    try:
        if args.files:
            with open(args.files) as handle:
                paths = [line.strip() for line in handle if line.strip()]
        else:
            paths = fetch_files(
                os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
                os.environ["GITHUB_REPOSITORY"],
                int(os.environ["PR_NUMBER"]),
                os.environ["GITHUB_TOKEN"],
            )
    except (KeyError, TypeError, ValueError, OSError, urllib.error.URLError) as error:
        print(f"pr-scope: could not read the PR's changed files ({error}); failing closed")
        return 1
    code, message = verdict(*classify(paths, args.config, args.source), override)
    print(f"pr-scope: {message}")
    return code


if __name__ == "__main__":
    sys.exit(main())
