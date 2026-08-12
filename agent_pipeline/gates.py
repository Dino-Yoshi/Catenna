"""Stage 4 brief/audit gate helpers."""

from __future__ import print_function

import shutil
import time

from . import usage
from .artifacts import CONTRACTS, parse_gate, sha256_file, validate_file
from .failures import (
    EXIT_BLOCKED,
    EXIT_SUCCESS,
    FAILURE_CLASS_GATE_PASS_LIMIT_EXHAUSTED,
    FAILURE_CLASS_GATE_REJECTED,
    FAILURE_CLASS_MALFORMED_ARTIFACT,
)
from .state import append_log, reconcile_artifacts


def run_stage4_gate_loop(task_dir, state, config, assignments, ensure_real_stage, block_transition, outcome_ledger_path=None):
    if "04_gate" in state.get("completed_stages", []) and accepted_stage4_gate(task_dir)["accepted"]:
        return EXIT_SUCCESS
    max_passes = int(config.get("max_gate_passes", 2))
    pass_number = len(state.get("stage_gate_passes") or []) + 1
    force_brief = pass_number > 1 or "04" not in state.get("completed_stages", [])
    force_audit = pass_number > 1 or "04_gate" not in state.get("completed_stages", [])
    previous_rejection = None
    while pass_number <= max_passes:
        code = ensure_real_stage(
            task_dir,
            state,
            config,
            "04",
            "read-only",
            assignments,
            pass_number=pass_number,
            force=force_brief,
            extra_context=stage4_rejected_gate_context(task_dir, state, pass_number),
        )
        if code != EXIT_SUCCESS:
            return code
        reconcile_artifacts(task_dir, state, read_only=False)
        archive_stage4_brief_pass(task_dir, pass_number)
        if previous_rejection:
            new_brief_hash = sha256_file(task_dir / CONTRACTS["04"].filename)
            current_audit_hash = sha256_file(task_dir / CONTRACTS["04_gate"].filename) if (task_dir / CONTRACTS["04_gate"].filename).exists() else None
            if new_brief_hash == previous_rejection["brief_hash"] and current_audit_hash == previous_rejection["audit_hash"]:
                block_transition(
                    task_dir,
                    state,
                    "04_gate",
                    "Stage 4 revision output is identical to a previously rejected brief with unchanged audit feedback",
                    FAILURE_CLASS_GATE_REJECTED,
                    completed_through="04",
                )
                return EXIT_BLOCKED
        code = ensure_real_stage(task_dir, state, config, "04_gate", "read-only", assignments, pass_number=pass_number, force=force_audit)
        if code != EXIT_SUCCESS:
            return code
        reconcile_artifacts(task_dir, state, read_only=False)

        gate = accepted_stage4_gate(task_dir)
        record_gate_pass(task_dir, state, pass_number, gate)
        append_log(task_dir, {"event": "stage4_gate_decision", "stage": "04_gate", "pass": pass_number, "accepted": bool(gate.get("accepted")), "valid": bool(gate.get("valid")), "classification": gate_classification(gate), "run_id": state.get("run_id")})
        if gate.get("valid"):
            record_stage4_quality_outcome(task_dir, state, config, pass_number, gate, outcome_ledger_path)
        if gate["accepted"]:
            return EXIT_SUCCESS
        if not gate.get("valid"):
            block_transition(task_dir, state, "04_gate", gate["reason"], FAILURE_CLASS_MALFORMED_ARTIFACT, completed_through="04")
            return EXIT_BLOCKED
        if pass_number >= max_passes:
            append_log(task_dir, {"event": "stage4_pass_exhaustion", "stage": "04_gate", "pass": pass_number, "classification": FAILURE_CLASS_GATE_PASS_LIMIT_EXHAUSTED, "run_id": state.get("run_id")})
            block_transition(task_dir, state, "04_gate", "Stage 4 gate remains rejected after %d bounded pass(es)" % max_passes, FAILURE_CLASS_GATE_PASS_LIMIT_EXHAUSTED, completed_through="04")
            return EXIT_BLOCKED

        previous_rejection = {
            "brief_hash": sha256_file(task_dir / CONTRACTS["04"].filename),
            "audit_hash": sha256_file(task_dir / CONTRACTS["04_gate"].filename),
        }
        append_log(task_dir, {"event": "retry_scheduled", "stage": "04", "pass": pass_number + 1, "classification": FAILURE_CLASS_GATE_REJECTED, "run_id": state.get("run_id")})
        force_brief = True
        pass_number += 1
        force_audit = True
    append_log(task_dir, {"event": "stage4_pass_exhaustion", "stage": "04_gate", "pass": pass_number - 1, "classification": FAILURE_CLASS_GATE_PASS_LIMIT_EXHAUSTED, "run_id": state.get("run_id")})
    block_transition(task_dir, state, "04_gate", "Stage 4 gate pass limit exhausted", FAILURE_CLASS_GATE_PASS_LIMIT_EXHAUSTED, completed_through="04")
    return EXIT_BLOCKED


def stage4_rejected_gate_context(task_dir, state, pass_number):
    if pass_number <= 1:
        return None
    passes = state.get("stage_gate_passes") or []
    if not passes:
        return None
    latest = passes[-1]
    if latest.get("accepted") is not False:
        return None
    gate_path = task_dir / CONTRACTS["04_gate"].filename
    try:
        gate_text = gate_path.read_text(encoding="utf-8")
    except Exception:
        return None
    return "\n".join([
        "Latest Stage 04 gate rejection feedback:",
        "",
        gate_text.rstrip(),
        "",
        "Revise Stage 04 using the feedback above.",
    ]).rstrip() + "\n"


def accepted_stage4_gate(task_dir):
    path = task_dir / CONTRACTS["04_gate"].filename
    validation = validate_file(path, "04_gate", read_only=True)
    if not validation["valid"]:
        return {"accepted": False, "reason": validation["reason"], "valid": False}
    parsed = parse_gate(path.read_text(encoding="utf-8"))
    if not parsed.get("valid"):
        return {"accepted": False, "reason": parsed["reason"], "valid": False}
    if parsed["gate"].get("ready_for_implementation") is not True:
        return {"accepted": False, "reason": "Stage 4 audit gate rejected implementation", "valid": True, "gate": parsed["gate"]}
    return {"accepted": True, "reason": "accepted", "valid": True, "gate": parsed["gate"]}


def record_gate_pass(task_dir, state, pass_number, gate):
    audit_path = task_dir / CONTRACTS["04_gate"].filename
    pass_path = task_dir / ("04_final_brief_audit.pass-%d.md" % pass_number)
    if audit_path.exists():
        shutil.copyfile(str(audit_path), str(pass_path))
    record = {
        "pass": pass_number,
        "accepted": bool(gate.get("accepted")),
        "reason": gate.get("reason"),
        "brief_hash": sha256_file(task_dir / CONTRACTS["04"].filename) if (task_dir / CONTRACTS["04"].filename).exists() else None,
        "audit_hash": sha256_file(audit_path) if audit_path.exists() else None,
        "recorded_at": now(),
    }
    existing = state.setdefault("stage_gate_passes", [])
    if not any(item.get("pass") == pass_number and item.get("audit_hash") == record["audit_hash"] for item in existing):
        existing.append(record)


def archive_stage4_brief_pass(task_dir, pass_number):
    brief_path = task_dir / CONTRACTS["04"].filename
    pass_path = task_dir / ("04_final_codex_brief.pass-%d.md" % pass_number)
    if brief_path.exists():
        shutil.copyfile(str(brief_path), str(pass_path))


def gate_classification(gate):
    if not gate.get("valid"):
        return FAILURE_CLASS_MALFORMED_ARTIFACT
    if gate.get("accepted"):
        return "accepted"
    return FAILURE_CLASS_GATE_REJECTED


def record_stage4_quality_outcome(task_dir, state, config, pass_number, gate, outcome_ledger_path=None):
    def log_write_failed(reason=None, error=None):
        event = {
            "event": "stage4_quality_outcome_write_failed",
            "stage": "04_gate",
            "pass": pass_number,
            "run_id": state.get("run_id"),
        }
        if reason is not None:
            event["reason"] = reason
        if error is not None:
            event["error"] = error
        try:
            append_log(task_dir, event)
        except Exception:
            pass

    try:
        if not outcome_ledger_path:
            return False
        if not config.get("cost_control", {}).get("quality_aware", False):
            return False
        agent, model = finalized_stage4_producer(task_dir, state, pass_number)
        if model is None:
            return False
        entry = usage.build_outcome_entry(
            state.get("task"),
            state.get("run_id"),
            "04",
            agent,
            model,
            pass_number,
            bool(gate.get("accepted")),
            gate_classification(gate),
        )
        written = usage.append_entry(outcome_ledger_path, entry)
        if not written:
            log_write_failed(reason="append_entry_returned_false")
            return False
        return True
    except Exception as exc:
        log_write_failed(error=str(exc))
        return False


def finalized_stage4_producer(task_dir, state, pass_number):
    runs = state.get("real_stage_runs", {}).get("04") or []
    finalized = [
        run for run in runs
        if run.get("pass_number") == pass_number and run.get("finalized") is True
    ]
    if not finalized:
        return None, None
    brief_path = task_dir / CONTRACTS["04"].filename
    brief_hash = None
    try:
        if brief_path.exists():
            brief_hash = sha256_file(brief_path)
    except Exception:
        brief_hash = None
    if brief_hash:
        for run in reversed(finalized):
            if run.get("final_artifact_hash") == brief_hash:
                return run.get("agent"), run.get("model")
    selected = finalized[-1]
    return selected.get("agent"), selected.get("model")


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
