#!/usr/bin/env python3
"""Report one docker image build's footprint via OTLP/HTTP+JSON.

Two exports, deliberately split on cardinality:
  - POST {endpoint}/v1/metrics — build duration, image size, layer count
    as OTLP gauge data points. Attributes are bounded-cardinality only
    (image name without tag, pipeline/job name) so these compose safely
    as metric dimensions in any real time-series backend downstream of an
    OTel Collector.
  - POST {endpoint}/v1/logs — one OTLP log record per build carrying the
    high-cardinality identifiers a metric attribute must never carry
    (git_sha, workflow run id, run URL, the full tagged image ref), plus
    the same three measurements repeated for convenient correlation.

Deliberately stdlib-only: every repo's self-hosted runner already has
python3 on PATH (see ci.yml's Arch-runner comments), so this has no install
step of its own and can be copied verbatim into any repo's CI — no
`opentelemetry-sdk` dependency, just hand-built OTLP/HTTP+JSON payloads
(the wire format is a documented JSON schema; nothing here needs protobuf).
Uses http.client rather than urllib.request.urlopen: Semgrep's
python.lang.security.audit.dynamic-urllib-use-detected rule blocks urlopen
on a non-literal URL (movingfirm-frontend#75 CI), and OTEL_EXPORTER_OTLP_*
env vars are non-literal by design.

Never raises and never exits non-zero — a telemetry export failing must
not fail a build, so every error path degrades to a GitHub Actions
::warning:: and exit 0.

All inputs come from the environment (never argv), so the calling workflow
step just sets env: and runs `python3 scripts/report_build_telemetry.py`.

Required:
    OTEL_EXPORTER_OTLP_ENDPOINT   Base OTLP/HTTP endpoint (e.g.
                                  https://admin.example.com); this appends
                                  /v1/metrics and /v1/logs itself, same as
                                  a real OTel SDK exporter would. Unset ->
                                  no-op, by design: this is the switch
                                  each repo's own secret is left empty
                                  until it should start reporting — and
                                  the same switch that lets this endpoint
                                  be swapped for a real OTel Collector
                                  later with zero producer-side changes.
    IMAGE_REF                     Image ref as passed to `docker
                                  build`/`buildx build --tag` (e.g.
                                  ghcr.io/ieepirzy/x:sha-abc).
    REPO, GIT_SHA                 github.repository / github.sha.

Optional:
    OTEL_EXPORTER_OTLP_HEADERS    Comma-separated key=value pairs, value
                                  percent-encoded — the standard OTel
                                  env-var format (e.g.
                                  "Authorization=Bearer%20<token>").
    IMAGE_DIGEST                  The pushed digest (build-push-action's
                                  `outputs.digest`). Set -> inspect the
                                  image via the registry (`docker buildx
                                  imagetools inspect`), no local pull
                                  needed. Unset -> the build only loaded a
                                  local image (push: false / plain
                                  `docker build`), so inspect it locally
                                  instead (`docker image inspect`).
    BUILD_STARTED_AT              Unix timestamp (float) captured by a
                                  step immediately before the build step.
                                  Unset -> build_duration_seconds is
                                  omitted rather than guessed.
    REF_NAME, RUN_ID, RUN_URL, JOB_NAME
                                  Context fields for the log record —
                                  vcs.ref.head.name, cicd.pipeline.run.id,
                                  url.full, cicd.pipeline.name. JOB_NAME
                                  also appears on the metrics as
                                  cicd.pipeline.name (bounded — it's a
                                  fixed job name, not a run id).
"""
import http.client
import json
import os
import subprocess
import sys
import time
import urllib.parse


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


def _split_image_tag(image_ref: str) -> tuple[str, str | None]:
    """ghcr.io/x/y:sha-abc -> ("ghcr.io/x/y", "sha-abc"); mirarun:ci ->
    ("mirarun", "ci"); untagged -> (image_ref, None). Assumes no port in
    the registry host, true for every ref this fleet builds today."""
    if ":" in image_ref:
        name, tag = image_ref.rsplit(":", 1)
        return name, tag
    return image_ref, None


def _parse_otlp_headers(raw: str) -> dict:
    """OTEL_EXPORTER_OTLP_HEADERS: comma-separated key=value pairs, value
    percent-encoded — the OTel SDK env-var spec's format."""
    headers = {}
    for pair in (raw or "").split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        headers[key.strip()] = urllib.parse.unquote(value.strip())
    return headers


def _post_json(base_url: str, path: str, headers: dict, body: dict) -> int:
    """POSTs JSON via http.client, not urllib.request.urlopen — see the
    module docstring on why. Returns the HTTP status code; raises on any
    connection-level failure (caught by every caller)."""
    parsed = urllib.parse.urlsplit(base_url.rstrip("/") + path)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported OTLP endpoint scheme {parsed.scheme!r} (only http/https)")
    conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(parsed.netloc, timeout=10)
    try:
        request_path = parsed.path or "/"
        if parsed.query:
            request_path = f"{request_path}?{parsed.query}"
        payload = json.dumps(body).encode("utf-8")
        request_headers = {"Content-Type": "application/json", "Content-Length": str(len(payload))}
        request_headers.update(headers)
        conn.request("POST", request_path, body=payload, headers=request_headers)
        response = conn.getresponse()
        response.read()
        return response.status
    finally:
        conn.close()


def _resource(repo: str) -> dict:
    return {"attributes": [
        {"key": "service.name", "value": {"stringValue": repo.split("/")[-1]}},
        {"key": "service.namespace", "value": {"stringValue": "muutto365-fleet"}},
        {"key": "vcs.repository.name", "value": {"stringValue": repo}},
    ]}


def _metric(name: str, unit: str, value, value_kind: str, image_name: str, job: str | None, time_unix_nano: int) -> dict | None:
    if value is None:
        return None
    attrs = [{"key": "container.image.name", "value": {"stringValue": image_name}}]
    if job:
        attrs.append({"key": "cicd.pipeline.name", "value": {"stringValue": job}})
    data_point = {"timeUnixNano": str(time_unix_nano), "attributes": attrs}
    if value_kind == "int":
        data_point["asInt"] = str(int(value))  # protobuf JSON mapping: int64 as string
    else:
        data_point["asDouble"] = float(value)
    return {"name": name, "unit": unit, "gauge": {"dataPoints": [data_point]}}


def _log_record(*, image_name, image_tag, git_sha, ref_name, run_id, run_url, job,
                 duration, size_bytes, layer_count, time_unix_nano) -> dict:
    attrs = [{"key": "vcs.ref.head.revision", "value": {"stringValue": git_sha}},
             {"key": "container.image.name", "value": {"stringValue": image_name}}]
    if image_tag:
        attrs.append({"key": "container.image.tag", "value": {"stringValue": image_tag}})
    if ref_name:
        attrs.append({"key": "vcs.ref.head.name", "value": {"stringValue": ref_name}})
    if run_id:
        attrs.append({"key": "cicd.pipeline.run.id", "value": {"stringValue": run_id}})
    if run_url:
        attrs.append({"key": "url.full", "value": {"stringValue": run_url}})
    if job:
        attrs.append({"key": "cicd.pipeline.name", "value": {"stringValue": job}})
    if duration is not None:
        attrs.append({"key": "ci.build.duration_seconds", "value": {"doubleValue": float(duration)}})
    if size_bytes is not None:
        attrs.append({"key": "ci.build.image.size_bytes", "value": {"intValue": str(int(size_bytes))}})
    if layer_count is not None:
        attrs.append({"key": "ci.build.image.layer_count", "value": {"intValue": str(int(layer_count))}})
    return {
        "timeUnixNano": str(time_unix_nano),
        "severityText": "INFO",
        "body": {"stringValue": f"docker image build reported: {image_name}:{image_tag or '(untagged)'}"},
        "attributes": attrs,
    }


def main() -> None:
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        notice("OTEL_EXPORTER_OTLP_ENDPOINT not set, skipping")
        return

    image_ref = os.environ["IMAGE_REF"]
    digest = os.environ.get("IMAGE_DIGEST") or ""
    repo = os.environ["REPO"]
    git_sha = os.environ["GIT_SHA"]
    job = os.environ.get("JOB_NAME")

    # The measurement clock is captured BEFORE image inspection, which does
    # one or two registry round trips (docker buildx imagetools inspect,
    # each up to a 30s timeout) — measuring after would fold that latency
    # into build_duration_seconds and overstate short/cached builds by as
    # much as a minute (Codex review, movingfirm-admin#120).
    build_completed_at = time.time()
    size_bytes, layer_count = inspect_image(image_ref, digest)

    duration = None
    started_at = os.environ.get("BUILD_STARTED_AT")
    if started_at:
        try:
            duration = round(build_completed_at - float(started_at), 1)
        except ValueError:
            warn(f"BUILD_STARTED_AT={started_at!r} is not a number, omitting build_duration_seconds")

    image_name, image_tag = _split_image_tag(image_ref)
    time_unix_nano = int(build_completed_at * 1_000_000_000)
    resource = _resource(repo)
    headers = _parse_otlp_headers(os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", ""))

    metrics = [m for m in (
        _metric("ci.build.duration_seconds", "s", duration, "double", image_name, job, time_unix_nano),
        _metric("ci.build.image.size_bytes", "By", size_bytes, "int", image_name, job, time_unix_nano),
        _metric("ci.build.image.layer_count", "1", layer_count, "int", image_name, job, time_unix_nano),
    ) if m is not None]

    if metrics:
        metrics_body = {"resourceMetrics": [{
            "resource": resource,
            "scopeMetrics": [{"scope": {"name": "report_build_telemetry", "version": "1"}, "metrics": metrics}],
        }]}
        try:
            status = _post_json(endpoint, "/v1/metrics", headers, metrics_body)
            (notice if status < 300 else warn)(f"POST /v1/metrics for {image_ref}: {status}")
        except Exception as e:
            warn(f"POST {endpoint}/v1/metrics failed, continuing ({e})")
    else:
        notice("no non-null measurements to report as metrics")

    logs_body = {"resourceLogs": [{
        "resource": resource,
        "scopeLogs": [{"scope": {"name": "report_build_telemetry", "version": "1"}, "logRecords": [_log_record(
            image_name=image_name, image_tag=image_tag, git_sha=git_sha,
            ref_name=os.environ.get("REF_NAME"), run_id=os.environ.get("RUN_ID"),
            run_url=os.environ.get("RUN_URL"), job=job,
            duration=duration, size_bytes=size_bytes, layer_count=layer_count,
            time_unix_nano=time_unix_nano,
        )]}],
    }]}
    try:
        status = _post_json(endpoint, "/v1/logs", headers, logs_body)
        (notice if status < 300 else warn)(f"POST /v1/logs for {image_ref}: {status}")
    except Exception as e:
        warn(f"POST {endpoint}/v1/logs failed, continuing ({e})")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Belt and braces: this script must never fail the build it's
        # reporting on, however it breaks.
        warn(f"unexpected error, continuing ({e})")
    sys.exit(0)
