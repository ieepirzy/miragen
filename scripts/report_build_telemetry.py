#!/usr/bin/env python3
"""Report one docker image build's footprint to the fleet's build-telemetry
endpoint (POST /api/ci/build-telemetry in movingfirm-admin) — image size,
layer count, build duration.

Deliberately stdlib-only: every repo's self-hosted runner already has
python3 on PATH (see ci.yml's Arch-runner comments), so this has no install
step of its own and can be copied verbatim into any repo's CI. Never raises
and never exits non-zero — a telemetry POST failing must not fail a build,
so every error path degrades to a GitHub Actions ::warning:: and exit 0.

All inputs come from the environment (never argv), so the calling workflow
step just sets env: and runs `python3 scripts/report_build_telemetry.py`.

Required:
    BUILD_TELEMETRY_URL     Endpoint to POST to. Unset/empty -> no-op, by
                             design: this is the switch each repo's own
                             BUILD_TELEMETRY_URL secret is left empty until
                             it should start reporting.
    IMAGE_REF                Image ref as passed to `docker build`/`buildx
                             build --tag` (e.g. ghcr.io/ieepirzy/x:sha-abc).
    REPO, GIT_SHA             github.repository / github.sha.

Optional:
    BUILD_TELEMETRY_TOKEN    Bearer token for the endpoint.
    IMAGE_DIGEST              The pushed digest (build-push-action's
                             `outputs.digest`). Set -> inspect the image via
                             the registry (`docker buildx imagetools
                             inspect`), no local pull needed. Unset -> the
                             build only loaded a local image (push: false /
                             plain `docker build`), so inspect it locally
                             instead (`docker image inspect`).
    BUILD_STARTED_AT          Unix timestamp (float) captured by a step
                             immediately before the build step. Unset ->
                             build_duration_seconds is reported as null
                             rather than guessed.
    REF_NAME, RUN_ID, RUN_URL, JOB_NAME
                             Context fields, all optional in the payload.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request


def notice(msg: str) -> None:
    print(f"::notice::build-telemetry: {msg}")


def warn(msg: str) -> None:
    print(f"::warning::build-telemetry: {msg}")


def _run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30).stdout


def inspect_image(image_ref: str, digest: str) -> tuple[int | None, int | None]:
    """Returns (image_size_bytes, layer_count), best-effort — either can be
    None if inspection fails or the shape is one this doesn't handle
    (e.g. it gives up after one level of manifest-index indirection rather
    than resolving every platform)."""
    try:
        if digest:
            raw = _run(["docker", "buildx", "imagetools", "inspect", f"{image_ref}@{digest}", "--raw"])
            manifest = json.loads(raw)
            if "manifests" in manifest:
                # A multi-platform index — pick the first platform manifest
                # rather than resolving all of them. Every repo in this
                # fleet currently builds single-platform, so this branch is
                # untested against a real multi-platform image; if that
                # ever changes, the per-platform sizes should be reported
                # separately instead of silently picking one.
                sub_digest = manifest["manifests"][0]["digest"]
                raw = _run(["docker", "buildx", "imagetools", "inspect", f"{image_ref}@{sub_digest}", "--raw"])
                manifest = json.loads(raw)
            layers = manifest.get("layers", [])
            return sum(int(l.get("size", 0)) for l in layers), len(layers)
        else:
            out = _run(["docker", "image", "inspect", image_ref, "--format={{json .}}"])
            info = json.loads(out)
            layers = (info.get("RootFS") or {}).get("Layers") or []
            return info.get("Size"), len(layers)
    except Exception as e:
        warn(f"could not inspect {image_ref!r}, reporting without size/layers ({e})")
        return None, None


def main() -> None:
    url = os.environ.get("BUILD_TELEMETRY_URL")
    if not url:
        notice("BUILD_TELEMETRY_URL not set, skipping")
        return

    image_ref = os.environ["IMAGE_REF"]
    digest = os.environ.get("IMAGE_DIGEST") or ""
    size_bytes, layer_count = inspect_image(image_ref, digest)

    duration = None
    started_at = os.environ.get("BUILD_STARTED_AT")
    if started_at:
        try:
            duration = round(time.time() - float(started_at), 1)
        except ValueError:
            warn(f"BUILD_STARTED_AT={started_at!r} is not a number, omitting build_duration_seconds")

    payload = {
        "repo": os.environ["REPO"],
        "image": image_ref,
        "git_sha": os.environ["GIT_SHA"],
        "ref": os.environ.get("REF_NAME"),
        "workflow_run_id": os.environ.get("RUN_ID"),
        "run_url": os.environ.get("RUN_URL"),
        "job": os.environ.get("JOB_NAME"),
        "build_duration_seconds": duration,
        "image_size_bytes": size_bytes,
        "layer_count": layer_count,
    }

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ.get('BUILD_TELEMETRY_TOKEN', '')}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            notice(f"reported {image_ref} ({response.status})")
    except Exception as e:
        warn(f"POST to {url} failed, continuing ({e})")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Belt and braces: this script must never fail the build it's
        # reporting on, however it breaks.
        warn(f"unexpected error, continuing ({e})")
    sys.exit(0)
