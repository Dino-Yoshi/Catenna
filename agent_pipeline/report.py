"""Per-task legible report: synthesizes stage status, decision,
verification, usage, and reasoning-trace visibility into one document.

Today a human trying to understand "what happened on this task" has to run
pipeline-status, pipeline-verify, pipeline-usage, and pipeline-brief
separately and merge the output by hand. This module produces the merged
document instead. Structured the same way as verification.py:
generate_report(...) returns a dict with report_paths after writing the
report to disk; render_markdown/write_report are small, separately
testable helpers.

Deliberately reads task_dir directly for anything task-local (state,
artifact files, the verification report) rather than depending on
controller.py's path constants, matching how verification.py/usage.py stay
decoupled from controller.py. Ledger entries are passed in by the caller
(controller.py already owns the ledger's path) rather than read here.
"""

from __future__ import print_function

import json
import re
import time
from pathlib import Path

from .artifacts import CONTRACTS, extract_section, manual_test_decision, validate_file
from .state import STAGE_ORDER, orchestrator_dir
from . import tail as tail_module
from . import usage as usage_module


VERIFICATION_REPORT_FILENAME = "05_verification_report.json"
DECISION_FILENAME = CONTRACTS["08"].filename


def report_dir(task_dir):
    return orchestrator_dir(task_dir)


def generate_report(task_dir, task, state, usage_entries=None):
    stages = [_stage_row(task_dir, state, key) for key in STAGE_ORDER]
    report = {
        "schema_version": 1,
        "generated_at": now(),
        "task": task,
        "state": state.get("state"),
        "current_stage": state.get("current_stage"),
        "completed_stages": list(state.get("completed_stages") or []),
        "stages": stages,
        "decision": _decision_summary(task_dir),
        "verification": _verification_summary(task_dir),
        "usage": usage_module.summarize(usage_entries or [], group_by="agent"),
        "reasoning_traces": _reasoning_traces(task_dir),
        "fallback_events": list(state.get("fallback_events") or []),
    }
    paths = write_report(task_dir, report)
    report["report_paths"] = paths
    return report


def _stage_row(task_dir, state, stage_key):
    contract = CONTRACTS[stage_key]
    path = task_dir / contract.filename
    artifact_status = (state.get("artifact_status") or {}).get(contract.filename) or {}
    runs = (state.get("real_stage_runs") or {}).get(stage_key) or []
    last_run = runs[-1] if runs else None
    excerpt = None
    if path.exists() and artifact_status.get("status") == "valid":
        excerpt = _short_excerpt(path.read_text(encoding="utf-8", errors="replace"))
    return {
        "stage": stage_key,
        "filename": contract.filename,
        "status": artifact_status.get("status", "missing"),
        "reason": artifact_status.get("reason"),
        "agent": (state.get("stage_agents") or {}).get(stage_key),
        "duration_seconds": last_run.get("duration_seconds") if last_run else None,
        "failure_class": last_run.get("failure_class") if last_run else None,
        "excerpt": excerpt,
    }


def _short_excerpt(text, limit=300):
    """Excerpt of an artifact's body, skipping its required top-level
    heading line -- generic across all 8 contracts' differing section
    names rather than special-casing each one."""
    lines = text.splitlines()
    body = "\n".join(lines[1:]) if len(lines) > 1 else ""
    return _collapse_excerpt(body, limit=limit)


def _collapse_excerpt(text, limit=300):
    collapsed = re.sub(r"\s+", " ", text or "").strip()
    if not collapsed:
        return None
    return collapsed[:limit] + ("..." if len(collapsed) > limit else "")


def _decision_summary(task_dir):
    path = task_dir / DECISION_FILENAME
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    if not validate_file(path, "08", read_only=True)["valid"]:
        return None
    reason = extract_section(text, ("Reason",))
    return {
        "final_decision": manual_test_decision(text) or "unknown",
        "reason_excerpt": _collapse_excerpt(reason),
    }


def _verification_summary(task_dir):
    path = task_dir / VERIFICATION_REPORT_FILENAME
    if not path.exists():
        return None
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    checks = [
        {"name": check.get("name"), "status": check.get("status")}
        for check in report.get("checks", [])
    ]
    return {
        "overall_status": report.get("overall_status"),
        "checks": checks,
        "test_coverage_delta_signal": (report.get("test_coverage_delta_signal") or {}).get("status"),
    }


def _reasoning_traces(task_dir):
    directory = tail_module.runs_dir(task_dir)
    if not directory.exists():
        return []
    traces = []
    for reasoning_path in sorted(directory.glob("*.reasoning.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        base = reasoning_path.name[: -len(".reasoning.md")]
        metadata_path = directory / (base + ".json")
        metadata = {}
        if metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except Exception:
                metadata = {}
        text = reasoning_path.read_text(encoding="utf-8", errors="replace")
        traces.append({
            "run": base,
            "stage": metadata.get("stage"),
            "agent": metadata.get("agent"),
            "run_id": metadata.get("run_id"),
            "path": str(reasoning_path),
            "excerpt": _collapse_excerpt(text),
        })
    return traces


def write_report(task_dir, report):
    directory = report_dir(task_dir)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "task_report.json"
    md_path = directory / "task_report.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return {"json_path": str(json_path), "md_path": str(md_path)}


def render_markdown(report):
    lines = [
        "# Pipeline report — %s" % report.get("task"),
        "",
        "Generated: " + report.get("generated_at", ""),
        "State: **%s** | Current stage: **%s**" % (report.get("state"), report.get("current_stage") or "-"),
        "Completed stages: " + (", ".join(report.get("completed_stages") or []) or "none"),
        "",
        "## Stages",
        "",
        "| Stage | Status | Agent | Duration | Failure |",
        "|-------|--------|-------|----------|---------|",
    ]
    for row in report.get("stages", []):
        duration = "%.1fs" % row["duration_seconds"] if row.get("duration_seconds") is not None else "-"
        lines.append(
            "| %s | %s | %s | %s | %s |"
            % (row["stage"], row["status"], row.get("agent") or "-", duration, row.get("failure_class") or "-")
        )
    for row in report.get("stages", []):
        if row.get("excerpt"):
            lines.append("")
            lines.append("**%s excerpt:** %s" % (row["stage"], row["excerpt"]))

    decision = report.get("decision")
    lines.extend(["", "## Decision", ""])
    if decision:
        lines.append("Final decision: **%s**" % decision["final_decision"])
        if decision.get("reason_excerpt"):
            lines.append("")
            lines.append("Reason: " + decision["reason_excerpt"])
    else:
        lines.append("Not yet decided (Stage 8 not complete).")

    verification = report.get("verification")
    lines.extend(["", "## Verification", ""])
    if verification:
        lines.append("Overall status: **%s**" % verification.get("overall_status"))
        for check in verification.get("checks", []):
            lines.append("- %s: %s" % (check.get("name"), check.get("status")))
        if verification.get("test_coverage_delta_signal"):
            lines.append("Test-coverage-delta signal: " + verification["test_coverage_delta_signal"])
    else:
        lines.append("No verification report recorded for this task yet.")

    usage_summary = report.get("usage") or {"groups": {}, "overall": {"count": 0}}
    lines.extend(["", "## Usage", ""])
    if usage_summary["groups"]:
        for name in sorted(usage_summary["groups"]):
            bucket = usage_summary["groups"][name]
            tokens = "in=%d out=%d" % (bucket["input_tokens"], bucket["output_tokens"]) if bucket["tokens_known"] else "tokens=unknown"
            cost = ("$%.4f" % bucket["total_cost_usd"]) if bucket["cost_known"] else "cost=unknown"
            lines.append("- %s: calls=%d failures=%d duration=%.1fs %s %s" % (name, bucket["count"], bucket["failures"], bucket["duration_seconds"], tokens, cost))
    else:
        lines.append("No usage ledger entries recorded for this task yet.")

    traces = report.get("reasoning_traces") or []
    lines.extend(["", "## Reasoning traces", ""])
    if traces:
        for trace in traces:
            lines.append("- stage %s, %s, run %s: %s" % (trace.get("stage") or "?", trace.get("agent") or "?", trace.get("run_id") or "?", trace.get("path")))
            if trace.get("excerpt"):
                lines.append("  " + trace["excerpt"])
    else:
        lines.append("No reasoning traces captured for this task yet.")

    fallback_events = report.get("fallback_events") or []
    lines.extend(["", "## Fallback / retry history", ""])
    if fallback_events:
        for event in fallback_events:
            lines.append("- " + json.dumps(event, sort_keys=True))
    else:
        lines.append("No fallback/retry events recorded.")

    return "\n".join(lines).rstrip() + "\n"


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
