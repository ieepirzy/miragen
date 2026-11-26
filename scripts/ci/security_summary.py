#!/usr/bin/env python3
"""Summarise SAST and DAST reports for the Security workflow.

Reads whichever reports exist (Semgrep JSON, Bandit JSON, ZAP JSON), writes
a Markdown table to the job summary, prints one line per finding, and
decides the step's exit code.

Blocking findings, once enforcement is on:
  - Semgrep: severity ERROR
  - Bandit: severity HIGH (the scan already drops LOW confidence)
  - ZAP: risk High (riskcode 3)

Report-only until SECURITY_ENFORCE is "true" (Loimi decision 1a469678,
2026-11-26: one week report-only, then block on high severity). A scanner
that produced no report while one was expected is an error in both modes,
so a broken scan never reads as a clean one.

Stdlib only: it runs on the runner's system Python, which has no pip.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Finding:
    tool: str
    severity: str
    rule: str
    where: str
    message: str
    blocking: bool


def _load(path: Path) -> dict | None:
    if not path.is_file() or path.stat().st_size == 0:
        return None
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def semgrep_findings(report: dict) -> list[Finding]:
    found = []
    for result in report.get("results", []):
        extra = result.get("extra", {})
        severity = str(extra.get("severity", "INFO")).upper()
        found.append(
            Finding(
                tool="semgrep",
                severity=severity,
                rule=str(result.get("check_id", "")).rsplit(".", 1)[-1],
                where=f"{result.get('path')}:{result.get('start', {}).get('line')}",
                message=" ".join(str(extra.get("message", "")).split())[:200],
                blocking=severity == "ERROR",
            )
        )
    return found


def bandit_findings(report: dict) -> list[Finding]:
    found = []
    for result in report.get("results", []):
        severity = str(result.get("issue_severity", "LOW")).upper()
        found.append(
            Finding(
                tool="bandit",
                severity=severity,
                rule=f"{result.get('test_id')} {result.get('test_name')}",
                where=f"{result.get('filename')}:{result.get('line_number')}",
                message=" ".join(str(result.get("issue_text", "")).split())[:200],
                blocking=severity == "HIGH",
            )
        )
    return found


_ZAP_RISK = {"0": "INFO", "1": "LOW", "2": "MEDIUM", "3": "HIGH"}


def zap_findings(report: dict) -> list[Finding]:
    found = []
    for site in report.get("site", []):
        for alert in site.get("alerts", []):
            risk = _ZAP_RISK.get(str(alert.get("riskcode")), "INFO")
            instances = alert.get("instances", [])
            first = instances[0].get("uri", "") if instances else ""
            where = f"{first} (+{len(instances) - 1} more)" if len(instances) > 1 else first
            found.append(
                Finding(
                    tool="zap",
                    severity=risk,
                    rule=f"{alert.get('pluginid')} {alert.get('name') or alert.get('alert')}",
                    where=where,
                    message=f"{len(instances)} instance(s)",
                    blocking=risk == "HIGH",
                )
            )
    return found


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render(findings: list[Finding], *, title: str, enforce: bool, missing: list[str]) -> str:
    blocking = [f for f in findings if f.blocking]
    mode = "blocking on high severity" if enforce else "report-only (not failing the job)"
    lines = [f"## {title}", "", f"Mode: **{mode}**.", ""]
    for name in missing:
        lines.append(f"- :x: **{name} produced no report.** The scan did not complete.")
    if missing:
        lines.append("")
    if not findings:
        lines.append("No findings.")
        return "\n".join(lines) + "\n"
    by_tool: dict[str, int] = {}
    for finding in findings:
        by_tool[finding.tool] = by_tool.get(finding.tool, 0) + 1
    counts = ", ".join(f"{tool}: {count}" for tool, count in sorted(by_tool.items()))
    lines += [f"{len(findings)} finding(s) ({counts}); {len(blocking)} high severity.", ""]
    lines += ["| Tool | Severity | Rule | Where | Detail |", "|---|---|---|---|---|"]
    order = {"ERROR": 0, "HIGH": 0, "WARNING": 1, "MEDIUM": 1, "LOW": 2, "INFO": 3}
    for f in sorted(findings, key=lambda x: (order.get(x.severity, 4), x.tool, x.where)):
        lines.append(
            f"| {f.tool} | {f.severity} | {_cell(f.rule)} | `{_cell(f.where)}` | {_cell(f.message)} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--title", required=True)
    parser.add_argument("--semgrep", type=Path)
    parser.add_argument("--bandit", type=Path)
    parser.add_argument("--zap", type=Path)
    args = parser.parse_args(argv)

    enforce = os.environ.get("SECURITY_ENFORCE", "false").strip().lower() == "true"
    findings: list[Finding] = []
    missing: list[str] = []
    for name, path, parse in (
        ("Semgrep", args.semgrep, semgrep_findings),
        ("Bandit", args.bandit, bandit_findings),
        ("ZAP", args.zap, zap_findings),
    ):
        if path is None:
            continue
        report = _load(path)
        if report is None:
            missing.append(name)
            continue
        findings += parse(report)

    summary = render(findings, title=args.title, enforce=enforce, missing=missing)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as handle:
            handle.write(summary)
    for f in findings:
        level = "error" if f.blocking and enforce else "warning"
        print(f"::{level}::{f.tool} {f.severity} {f.rule} at {f.where}: {f.message}")

    if missing:
        print(f"::error::no report from: {', '.join(missing)}")
        return 1
    blocking = [f for f in findings if f.blocking]
    if enforce and blocking:
        print(f"::error::{len(blocking)} high-severity finding(s)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
