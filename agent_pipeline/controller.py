"""Deterministic mock pipeline controller."""

from __future__ import print_function

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .artifacts import CONTRACTS, manual_test_decision, parse_gate, sha256_file, useful_partial, validate_file, validate_text
from . import color
from .config import ConfigError, configured_candidates, agent_config, load_config
from .failures import (
    BANNED_COMMAND_WORDS,
    EXIT_BAD_INPUT,
    EXIT_BLOCKED,
    EXIT_INTERRUPTED,
    EXIT_LOCKED,
    EXIT_SUCCESS,
    EXIT_VALIDATION,
    FAILURE_CLASS_EMPTY_OUTPUT,
    FAILURE_CLASS_GATE_PASS_LIMIT_EXHAUSTED,
    FAILURE_CLASS_GATE_REJECTED,
    FAILURE_CLASS_MALFORMED_ARTIFACT,
    FAILURE_CLASS_MALFORMED_OVERSEER,
    FAILURE_CLASS_MAX_TURNS,
    FAILURE_CLASS_PERMISSION_ERROR,
    FAILURE_CLASS_PROCESS_INTERRUPTED,
    FAILURE_CLASS_RATE_LIMIT,
    FAILURE_CLASS_SANDBOX_ENVIRONMENT,
    FAILURE_CLASS_SOURCE_FAILURE,
    FAILURE_CLASS_STAGE5_AMBIGUITY,
    FAILURE_CLASS_TIMEOUT,
    FAILURE_CLASS_UNKNOWN_FAILURE,
    FAILURE_CLASS_USAGE_LIMIT,
)
from .locking import LockError, TaskLock, explicit_unlock
from .mock_agent import MockAgent, valid_artifact
from .manifest import capture_dirty_baseline, git_status, validate_manifest, write_manifest
from .overseer import fallback_handoff, parse_overseer_candidate, upgrade_to_auto_verified, write_handoff_files
from .policies import choose_agent
from .prompts import render_prompt
from .real_runner import invoke_agent
from .runner import atomic_finalize, preserve_failed
from .state import CorruptState, STAGE_ORDER, append_log, load_state, new_state, orchestrator_dir, reconcile_artifacts, write_state_atomic
from . import report as report_module
from . import tail as tail_module
from . import usage
from . import verification


REPO_ROOT = Path.cwd()
TASKS_ROOT = REPO_ROOT / ".agent-pipeline" / "tasks"
# Mock scenario fixtures describe this controller's own state machine, not
# anything about the project being driven, so they live with the package
# (independent of which project's directory is REPO_ROOT/cwd) rather than
# under the driven project's .agent-pipeline/.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_ROOT = PACKAGE_ROOT / "fixtures"
SCENARIO_PATH = FIXTURES_ROOT / "mock_scenarios.json"
USAGE_ROOT = REPO_ROOT / ".agent-pipeline" / "usage"


def usage_ledger_path():
    return USAGE_ROOT / "ledger.jsonl"


def cooldown_store_path():
    return USAGE_ROOT / "agent_cooldowns.json"


class ControllerError(Exception):
    def __init__(self, message, exit_code=EXIT_BAD_INPUT):
        Exception.__init__(self, message)
        self.exit_code = exit_code


TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def validate_task_id(task):
    if not isinstance(task, str) or not TASK_ID_RE.match(task):
        raise ControllerError("invalid task id: %r" % (task,), EXIT_BAD_INPUT)
    return task


def task_dir_for(task):
    task = validate_task_id(task)
    root = TASKS_ROOT.resolve()
    candidate = (TASKS_ROOT / task).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ControllerError("invalid task id: %r" % (task,), EXIT_BAD_INPUT)
    return candidate


def current_task_path():
    return REPO_ROOT / ".agent-pipeline" / "current-task"


def read_current_task():
    """Return the persisted current-task name, or None if unset/unreadable.
    Best-effort, never raises (mirrors usage.load_cooldowns)."""
    try:
        text = current_task_path().read_text(encoding="utf-8").strip()
    except Exception:
        return None
    return text or None


def write_current_task(task):
    task = validate_task_id(task)
    path = current_task_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp.%d" % os.getpid())
    with open(str(tmp), "w", encoding="utf-8") as handle:
        handle.write(task + "\n")
    os.replace(str(tmp), str(path))


def resolve_task(explicit_task):
    """Return (task, used_default). Falls back to the persisted current-task
    pointer when explicit_task is not given; raises ControllerError if
    neither is available."""
    if explicit_task:
        return explicit_task, False
    task = read_current_task()
    if task:
        return task, True
    raise ControllerError(
        "no task given and no current task set — pass a task name, or run 'catenna use <task>' first (see 'catenna tasks')",
        EXIT_BAD_INPUT,
    )


def use_task(task):
    """CLI-facing: set or show the current-task pointer. Setting is
    permissive: tasks are created lazily elsewhere, so the task directory
    need not exist yet -- warn rather than block."""
    if not task:
        current = read_current_task()
        if current:
            print("current task: %s" % current)
        else:
            print("no current task set")
        return EXIT_SUCCESS
    task_dir = task_dir_for(task)
    if not task_dir.exists():
        print("warning: task directory does not exist yet for %r (will be created when the task runs)" % task)
    write_current_task(task)
    print("current task set to: %s" % task)
    return EXIT_SUCCESS


def list_tasks(plain=False):
    """CLI-facing: list every task directory under TASKS_ROOT with its
    state, marking whichever matches the current-task pointer.

    plain=True prints just the sorted task names, one per line, no
    marker/state/color -- used by shell completion to enumerate task names."""
    if not TASKS_ROOT.exists():
        if not plain:
            print("no tasks found under %s" % TASKS_ROOT)
        return EXIT_SUCCESS
    task_names = sorted(p.name for p in TASKS_ROOT.iterdir() if p.is_dir())
    if not task_names:
        if not plain:
            print("no tasks found under %s" % TASKS_ROOT)
        return EXIT_SUCCESS
    if plain:
        for name in task_names:
            print(name)
        return EXIT_SUCCESS
    current = read_current_task()
    for name in task_names:
        if name == current:
            marker = color.bold(color.cyan("*"))
        else:
            marker = " "
        try:
            state = load_state(TASKS_ROOT / name, name)
            state_label = state["state"]
        except CorruptState:
            state_label = "CORRUPT"
        print("%s %s  %s" % (marker, name, color.colorize_state(state_label)))
    return EXIT_SUCCESS


def load_scenarios():
    with open(str(SCENARIO_PATH), "r", encoding="utf-8") as handle:
        data = json.load(handle)
    for name, scenario in data.get("scenarios", {}).items():
        validate_mock_fixture(name, scenario)
    return data.get("scenarios", {})


def validate_mock_fixture(name, scenario):
    for key in ("command", "agent_command", "commands"):
        value = scenario.get(key)
        if not value:
            continue
        values = value if isinstance(value, list) else [value]
        for command in values:
            words = str(command).split()
            if any(word in BANNED_COMMAND_WORDS or word.endswith(".sh") for word in words):
                raise ControllerError("mock fixture %s configures forbidden command: %s" % (name, command))


def status(task):
    task_dir = task_dir_for(task)
    try:
        state = load_state(task_dir, task)
    except CorruptState as exc:
        print("CORRUPT state for %s: %s" % (task, exc))
        return EXIT_VALIDATION
    reconcile_artifacts(task_dir, state, read_only=True)
    print("task: %s" % task)
    print("state: %s" % color.colorize_state(state["state"]))
    print("current_stage: %s" % state["current_stage"])
    print("completed_stages: %s" % ", ".join(state["completed_stages"]))
    if state.get("run_unavailable_agents"):
        print("run_unavailable_agents: " + json.dumps(state["run_unavailable_agents"], sort_keys=True))
    try:
        cooldowns = usage.load_cooldowns(cooldown_store_path())
        if cooldowns:
            print("cross_task_cooldowns: " + json.dumps(cooldowns, sort_keys=True))
    except Exception:
        pass
    if state.get("fallback_events"):
        print("fallback_events: " + json.dumps(state["fallback_events"], sort_keys=True))
    if state.get("pending_approval"):
        print("pending_approval: " + json.dumps(state["pending_approval"], sort_keys=True))
    if state.get("last_failure"):
        print("last_failure: " + json.dumps(state["last_failure"], sort_keys=True))
    if "08" in state.get("completed_stages", []):
        decision_path = task_dir / CONTRACTS["08"].filename
        if decision_path.exists():
            final_decision = manual_test_decision(decision_path.read_text(encoding="utf-8"))
            print("final_decision: %s" % (final_decision or "unknown"))
    return EXIT_SUCCESS


def dry_run(task):
    task_dir = task_dir_for(task)
    try:
        state = load_state(task_dir, task)
    except CorruptState as exc:
        print("CORRUPT state for %s: %s" % (task, exc))
        return EXIT_VALIDATION
    invalidated = reconcile_artifacts(task_dir, state, read_only=True)
    print("task: %s" % task)
    print("would_resume_at: %s" % state["current_stage"])
    print("completed_stages: %s" % ", ".join(state["completed_stages"]))
    print("artifact_status:")
    for stage_key in STAGE_ORDER:
        filename = CONTRACTS[stage_key].filename
        detail = state.get("artifact_status", {}).get(filename)
        if not detail:
            continue
        line = "  %s: stage=%s status=%s reason=%s" % (
            filename,
            detail.get("stage"),
            detail.get("status"),
            detail.get("reason"),
        )
        if detail.get("stale"):
            line += " stale=true"
        print(line)
    if invalidated:
        print("stale_downstream_stages: %s" % ", ".join(invalidated))
    if state.get("fallback_events"):
        print("recorded_fallbacks: " + json.dumps(state["fallback_events"], sort_keys=True))
    return EXIT_SUCCESS


def pipeline_tail(task, stage=None, run_id=None, verbose=False):
    task_dir = task_dir_for(task)
    result = tail_module.follow(task_dir, stage=stage, run_id=run_id, verbose=verbose)
    return EXIT_SUCCESS if result in ("complete", "interrupted", "timed_out") else EXIT_BLOCKED


def pipeline_brief(task, stage=None, run_id=None, verbose=False):
    task_dir = task_dir_for(task)
    result = tail_module.brief(task_dir, stage=stage, run_id=run_id, verbose=verbose)
    return EXIT_SUCCESS if result == "ok" else EXIT_BLOCKED


def pipeline_verify(task, run_build=False):
    task_dir = task_dir_for(task)
    try:
        config = load_config()
    except ConfigError as exc:
        print("invalid real-run config: %s" % exc)
        return EXIT_VALIDATION
    try:
        report = verification.run_verification(
            task_dir,
            REPO_ROOT,
            run_build=run_build,
            driven_project_commands=config.get("verification", {}).get("driven_project_commands", []),
            skip_self_check=config.get("verification", {}).get("skip_self_check", False),
            build_implies_compile=config.get("verification", {}).get("build_implies_compile", False),
        )
    except verification.VerificationError as exc:
        print(str(exc))
        return EXIT_LOCKED
    def pass_fail_color(status_value):
        return color.green(status_value) if status_value == "passed" else color.red(status_value)

    print("task: %s" % task)
    print("overall_status: %s" % pass_fail_color(report["overall_status"]))
    for check in report["checks"]:
        if "exit_code" in check:
            duration_seconds = check.get("duration_seconds")
            if duration_seconds is None:
                duration_seconds = 0.0
            print("  %s: %s (exit=%s, %.1fs)" % (check["name"], pass_fail_color(check["status"]), check["exit_code"], duration_seconds))
        else:
            print("  %s: %s (%s)" % (check["name"], pass_fail_color(check["status"]), check.get("reason", "")))
    signal = report["test_coverage_delta_signal"]
    print("test_coverage_delta_signal: %s" % signal["status"])
    if signal.get("flagged_paths"):
        print("  flagged: " + ", ".join(signal["flagged_paths"]))
    print("report: %s" % report["report_paths"]["md_path"])
    return EXIT_SUCCESS if report["overall_status"] == "passed" else EXIT_VALIDATION


def launch_background(task, argv_tail, log_name):
    task_dir = task_dir_for(task)
    orch = task_dir / ".orchestrator"
    orch.mkdir(parents=True, exist_ok=True)
    log_path = orch / log_name
    argv = [sys.executable, "-m", "agent_pipeline.cli"] + list(argv_tail)
    log_handle = open(str(log_path), "a", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            cwd=str(REPO_ROOT),
            start_new_session=True,
        )
    except Exception:
        log_handle.close()
        raise
    log_handle.close()
    print("started background command: %s" % " ".join(argv))
    print("child pid: %s" % proc.pid)
    print("log: %s" % log_path)
    return EXIT_SUCCESS


def pipeline_run_background(task, allow_dirty=False):
    argv_tail = ["run", task]
    if allow_dirty:
        argv_tail.append("--allow-dirty")
    code = launch_background(task, argv_tail, "background_run.log")
    print("follow with: catenna tail %s" % task)
    print("check status: catenna status %s" % task)
    print("for more detail: catenna report %s" % task)
    return code


def pipeline_verify_background(task, run_build=False):
    argv_tail = ["verify", task]
    if run_build:
        argv_tail.append("--build")
    code = launch_background(task, argv_tail, "background_verify.log")
    report_path = task_dir_for(task) / "05_verification_report.md"
    print("verification report: %s" % report_path)
    print("follow verification stdout with: catenna tail %s" % task)
    print("full background launcher log: %s" % (task_dir_for(task) / ".orchestrator" / "background_verify.log"))
    return code


def pipeline_usage(task=None, agent=None, since_hours=None):
    entries = usage.read_entries(usage_ledger_path())
    if task:
        entries = [entry for entry in entries if entry.get("task") == task]
    if agent:
        entries = [entry for entry in entries if entry.get("agent") == agent]
    if since_hours is not None:
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - float(since_hours) * 3600))
        entries = [entry for entry in entries if (entry.get("recorded_at") or "") >= cutoff]
    summary = usage.summarize(entries, group_by="agent")
    print("entries: %d" % len(entries))
    for name in sorted(summary["groups"]):
        bucket = summary["groups"][name]
        tokens = "in=%d out=%d" % (bucket["input_tokens"], bucket["output_tokens"]) if bucket["tokens_known"] else "tokens=unknown"
        cost = ("$%.4f" % bucket["total_cost_usd"]) if bucket["cost_known"] else "cost=unknown"
        print("  %s: calls=%d failures=%d duration=%.1fs %s %s %s" % (name, bucket["count"], bucket["failures"], bucket["duration_seconds"], tokens, cost, format_cache_hit(bucket)))
    overall = summary["overall"]
    print("overall: calls=%d failures=%d duration=%.1fs %s" % (overall["count"], overall["failures"], overall["duration_seconds"], format_cache_hit(overall)))
    try:
        cooldowns = usage.load_cooldowns(cooldown_store_path())
        if cooldowns:
            print("cross_task_cooldowns: " + json.dumps(cooldowns, sort_keys=True))
    except Exception:
        pass
    return EXIT_SUCCESS


def format_cache_hit(bucket):
    ratio = bucket.get("cache_hit_ratio")
    if ratio is None:
        return "cache_hit=unknown"
    return "cache_hit=%.1f%%" % (float(ratio) * 100.0)


def pipeline_report(task):
    task_dir = task_dir_for(task)
    try:
        state = load_state(task_dir, task)
    except CorruptState as exc:
        print("CORRUPT state for %s: %s" % (task, exc))
        return EXIT_VALIDATION
    reconcile_artifacts(task_dir, state, read_only=True)
    entries = [entry for entry in usage.read_entries(usage_ledger_path()) if entry.get("task") == task]
    report = report_module.generate_report(task_dir, task, state, usage_entries=entries)
    print(report_module.render_markdown(report), end="")
    return EXIT_SUCCESS


def unlock(task, reason):
    task_dir = task_dir_for(task)
    result = explicit_unlock(task_dir, reason)
    print(result["message"])
    return EXIT_SUCCESS


def approve_retry(task, approval_id):
    run_id = make_run_id()
    task_dir = task_dir_for(task)
    try:
        with TaskLock(task_dir, "approve-retry", run_id):
            state = load_state(task_dir, task)
            state["run_id"] = run_id
            pending = state.get("pending_approval")
            if not pending:
                print("no pending approval")
                return EXIT_BAD_INPUT
            if pending.get("approval_id") != approval_id:
                print("approval ID mismatch")
                return EXIT_BAD_INPUT
            if pending.get("approved") or pending.get("consumed"):
                print("approval already used")
                return EXIT_BAD_INPUT
            pending["approved"] = True
            pending["approved_at"] = now()
            append_log(task_dir, {"event": "approval_granted", "approval_id": approval_id, "run_id": run_id})
            write_state_atomic(task_dir, state)
            print("approval granted: %s" % approval_id)
            return EXIT_SUCCESS
    except LockError as exc:
        print(str(exc))
        return EXIT_LOCKED
    except CorruptState as exc:
        print("CORRUPT state for %s: %s" % (task, exc))
        return EXIT_VALIDATION


def mock_run(task, scenario_name):
    scenarios = load_scenarios()
    if scenario_name not in scenarios:
        print("unknown scenario: %s" % scenario_name)
        return EXIT_BAD_INPUT
    scenario = scenarios[scenario_name]
    run_id = make_run_id()
    task_dir = task_dir_for(task)
    try:
        with TaskLock(task_dir, "mock-run:%s" % scenario_name, run_id):
            state = load_state(task_dir, task)
            state["run_id"] = run_id
            begin_new_run(state)
            append_log(task_dir, {"event": "run_started", "scenario": scenario_name, "run_id": run_id})
            code = run_scenario(task_dir, task, state, scenario)
            write_state_atomic(task_dir, state)
            append_log(task_dir, {"event": "run_finished", "state": state["state"], "exit_code": code, "run_id": run_id})
            print("mock-run %s: %s" % (scenario_name, state["state"]))
            return code
    except LockError as exc:
        print(str(exc))
        return EXIT_LOCKED
    except CorruptState as exc:
        print("CORRUPT state for %s: %s" % (task, exc))
        return EXIT_VALIDATION


def pipeline_run(task, allow_dirty=False):
    run_id = make_run_id()
    task_dir = task_dir_for(task)
    try:
        config = load_config()
    except ConfigError as exc:
        print("invalid real-run config: %s" % exc)
        return EXIT_VALIDATION
    try:
        with TaskLock(task_dir, "run", run_id):
            state = load_state(task_dir, task)
            noop = checkpoint_noop_eligible(task_dir, state)
            if noop["eligible"]:
                print("pipeline-run %s: %s" % (task, state["state"]))
                return EXIT_BLOCKED
            state["run_id"] = run_id
            begin_new_run(state)
            append_log(task_dir, {"event": "real_run_started", "run_id": run_id})
            code = run_real_pipeline(task_dir, task, state, config, allow_dirty)
            write_state_atomic(task_dir, state)
            append_log(task_dir, {"event": "real_run_finished", "state": state["state"], "exit_code": code, "run_id": run_id})
            print("pipeline-run %s: %s" % (task, state["state"]))
            return code
    except LockError as exc:
        print(str(exc))
        return EXIT_LOCKED
    except CorruptState as exc:
        print("CORRUPT state for %s: %s" % (task, exc))
        return EXIT_VALIDATION


def run_real_pipeline(task_dir, task, state, config, allow_dirty):
    task_dir.mkdir(parents=True, exist_ok=True)
    for seed_stage in ("00", "01"):
        validation = validate_file(task_dir / CONTRACTS[seed_stage].filename, seed_stage, read_only=True)
        if not validation["valid"]:
            state["completed_stages"] = []
            block_transition(task_dir, state, seed_stage, "Stage %s is missing or invalid: %s" % (seed_stage, validation["reason"]), validation.get("failure_class"))
            return EXIT_BLOCKED

    reconcile_artifacts(task_dir, state, read_only=False)
    assignments = dict(state.get("stage_agents") or {})

    for stage_key in ("02", "03"):
        code = ensure_real_stage(task_dir, state, config, stage_key, "read-only", assignments, pass_number=1)
        if code != EXIT_SUCCESS:
            return code
        reconcile_artifacts(task_dir, state, read_only=False)

    code = run_stage4_gate_loop(task_dir, state, config, assignments)
    if code != EXIT_SUCCESS:
        return code
    reconcile_artifacts(task_dir, state, read_only=False)

    gate = accepted_stage4_gate(task_dir)
    if not gate["accepted"]:
        block_transition(task_dir, state, "04_gate", gate["reason"], FAILURE_CLASS_MALFORMED_ARTIFACT, completed_through="04")
        return EXIT_BLOCKED

    stage5_current_run = False
    if "05" not in state.get("completed_stages", []):
        if not allow_dirty and git_status(REPO_ROOT):
            block_transition(task_dir, state, "05", "Source working tree is not clean outside .agent-pipeline; rerun with --allow-dirty if intentional", FAILURE_CLASS_SOURCE_FAILURE)
            return EXIT_BLOCKED
        baseline = capture_dirty_baseline(REPO_ROOT)
        state["dirty_baseline"] = baseline
        code = ensure_real_stage(task_dir, state, config, "05", "workspace-write", assignments, pass_number=1)
        if code != EXIT_SUCCESS:
            return code
        state["dirty_baseline"] = baseline
        stage5_current_run = True
    else:
        baseline = state.get("dirty_baseline") or capture_dirty_baseline(REPO_ROOT)

    reconcile_artifacts(task_dir, state, read_only=False)
    if stage5_current_run:
        clamp_completed_prefix(state, "05")
        write_state_atomic(task_dir, state)

    if "06" not in state.get("completed_stages", []):
        report_check = stage5_report_provenance(task_dir, state)
        append_log(task_dir, {"event": "artifact_validation", "stage": "05", "valid": report_check["valid"], "classification": report_check.get("failure_class"), "run_id": state.get("run_id")})
        if not report_check["valid"]:
            block_transition(task_dir, state, "05", report_check["reason"], report_check.get("failure_class", FAILURE_CLASS_STAGE5_AMBIGUITY), completed_through="04_gate")
            return EXIT_BLOCKED

        post_check = stage5_postprocessing_complete(task_dir, state)
        if post_check["valid"] and not stage5_current_run:
            block_transition(task_dir, state, "05", "Stage 5 post-processing already exists but was not produced by this controller run; no adoption path is configured", FAILURE_CLASS_STAGE5_AMBIGUITY, completed_through="04_gate")
            return EXIT_BLOCKED
        if not stage5_current_run:
            reason = post_check["reason"]
            if not any_stage5_postprocessing_present(task_dir, state):
                reason = "Pre-existing Stage 5 report/provenance has no complete post-processing and no adoption path is configured"
            block_transition(task_dir, state, "05", reason, post_check.get("failure_class", FAILURE_CLASS_STAGE5_AMBIGUITY), completed_through="04_gate")
            return EXIT_BLOCKED

        try:
            manifest = write_manifest(task_dir, REPO_ROOT, state, report_check["run"], baseline)
            report_check["run"]["dirty_changed_files"] = manifest.get("changed_files", [])
            append_log(task_dir, {"event": "manifest_generation", "stage": "05", "run_id": state.get("run_id"), "path": str(task_dir / "05_implementation_manifest.json")})
        except Exception as exc:
            block_transition(task_dir, state, "05", "Stage 5 manifest is structurally invalid: " + str(exc), FAILURE_CLASS_MALFORMED_ARTIFACT, completed_through="04_gate")
            return EXIT_BLOCKED

        verification_report = None
        try:
            verification_report = verification.run_verification(
                task_dir,
                REPO_ROOT,
                allow_pid=os.getpid(),
                driven_project_commands=config.get("verification", {}).get("driven_project_commands", []),
                skip_self_check=config.get("verification", {}).get("skip_self_check", False),
                build_implies_compile=config.get("verification", {}).get("build_implies_compile", False),
            )
        except verification.VerificationError as exc:
            append_log(task_dir, {"event": "verification_error", "stage": "05", "reason": str(exc), "run_id": state.get("run_id")})

        handoff = run_overseer_or_fallback(task_dir, state, config, manifest, assignments, verification_report)

        auto_verified = False
        if handoff.get("route") == "auto_verified":
            result = atomic_finalize(task_dir, "06", render_auto_stage06_notes(verification_report))
            if result["finalized"]:
                auto_verified = True
                append_log(task_dir, {"event": "stage6_auto_verified", "stage": "06", "run_id": state.get("run_id")})
                reconcile_artifacts(task_dir, state, read_only=False)

        if not auto_verified:
            state["state"] = "awaiting_human_test"
            state["current_stage"] = "06"
            state["human_checkpoint"] = {
                "stage": "06",
                "created_at": now(),
                "reason": "manual test notes required",
                "noop_hashes": checkpoint_hashes(task_dir, state),
            }
            state["next_required_human_action"] = "Run manual Stage 6 testing and record 06_manual_test_notes.md."
            state["last_failure"] = None
            append_log(task_dir, {"event": "human_checkpoint_transition", "stage": "06", "run_id": state.get("run_id")})
            return EXIT_BLOCKED

    code = ensure_real_stage(task_dir, state, config, "07", "read-only", assignments, pass_number=1)
    if code != EXIT_SUCCESS:
        return code
    reconcile_artifacts(task_dir, state, read_only=False)

    code, final_decision = ensure_stage08_decision(task_dir, state)
    if code != EXIT_SUCCESS:
        return code
    reconcile_artifacts(task_dir, state, read_only=False)
    state["last_failure"] = None
    return EXIT_SUCCESS if final_decision == "accept" else EXIT_VALIDATION


def run_stage4_gate_loop(task_dir, state, config, assignments):
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


def ensure_real_stage(task_dir, state, config, stage_key, execution_mode, assignments, pass_number=1, force=False, extra_context=None):
    pending = state.get("pending_approval") or {}
    awaiting_retry_approval = state.get("state") == "awaiting_retry_approval"
    completed = stage_key in state.get("completed_stages", [])
    if not awaiting_retry_approval:
        if not force and completed:
            return EXIT_SUCCESS
    elif pending.get("stage") != stage_key:
        if completed and not force:
            return EXIT_SUCCESS
        return EXIT_BLOCKED
    else:
        if not pending.get("approved") or pending.get("consumed"):
            return EXIT_BLOCKED
        if completed:
            consume_approved_retry_if_present(state, stage_key)
            append_log(task_dir, {"event": "approval_consumed", "stage": stage_key, "run_id": state.get("run_id")})
            state["pending_approval"] = None
            return EXIT_SUCCESS
        consume_approved_retry_if_present(state, stage_key)
        append_log(task_dir, {"event": "approval_consumed", "stage": stage_key, "run_id": state.get("run_id")})
        state["state"] = "running"
    attempt_budget = int(config.get("stage_attempt_budget", 2))
    attempts = 0
    next_attempt_kind = "normal"
    next_retry_reason = "initial/no-retry"
    while attempts < attempt_budget:
        route = choose_real_agent(stage_key, state, config, assignments, execution_mode)
        if not route:
            block_transition(task_dir, state, stage_key, "no configured capable runner exists for required role/mode", FAILURE_CLASS_SOURCE_FAILURE)
            return EXIT_BLOCKED
        agent = route["agent"]
        if route.get("fallback") or route.get("degraded"):
            if next_attempt_kind == "normal":
                next_attempt_kind = "provider_fallback"
                next_retry_reason = "provider fallback"
            event = {
                "stage": stage_key,
                "agent": agent,
                "reason": route.get("reason") or "fallback",
                "timestamp": now(),
            }
            state.setdefault("fallback_events", []).append(event)
            state.setdefault("fallback_history", []).append(event)
            append_log(task_dir, {"event": "provider_fallback_selected", "stage": stage_key, "provider": agent, "classification": event["reason"], "run_id": state["run_id"]})
        attempts += 1
        attempt_number = increment_attempt(state, stage_key)
        increment_agent_count(state, agent)
        append_log(task_dir, {"event": "stage_dispatch", "stage": stage_key, "pass": pass_number, "attempt": attempt_number, "provider": agent, "attempt_kind": next_attempt_kind, "retry_reason": next_retry_reason, "run_id": state.get("run_id")})
        result = invoke_stage(task_dir, state, config, stage_key, execution_mode, agent, pass_number, attempt_number=attempt_number, attempt_kind=next_attempt_kind, retry_reason=next_retry_reason, extra_context=extra_context)
        state.setdefault("real_stage_runs", {}).setdefault(stage_key, []).append(result)
        state.setdefault("execution_modes", {})[stage_key] = execution_mode
        source_after = None
        if execution_mode == "read-only":
            source_after = source_snapshot()
            if result.get("_source_before") != source_after:
                preserve_failed(task_dir, stage_key, read_candidate(result), "read_only_mutation", {"agent": agent, "metadata_path": result.get("metadata_path")})
                block_transition(task_dir, state, stage_key, "read-only agent mutated source files outside .agent-pipeline", FAILURE_CLASS_SANDBOX_ENVIRONMENT)
                return EXIT_BLOCKED
        raw_output = read_candidate(result)
        output = normalize_stage_output(stage_key, raw_output)
        final = atomic_finalize(task_dir, stage_key, output, read_only=True)
        append_log(task_dir, {"event": "stage_attempt_result", "stage": stage_key, "pass": pass_number, "attempt": attempt_number, "provider": agent, "classification": result.get("failure_class") or final["validation"].get("failure_class") or "success", "finalized": bool(final.get("finalized")), "run_id": state.get("run_id")})
        if final["finalized"]:
            result["finalized"] = True
            result["final_artifact_path"] = final["path"]
            result["final_artifact_hash"] = sha256_file(Path(final["path"]))
            if stage_key == "05":
                result["dirty_baseline"] = state.get("dirty_baseline")
            assignments[stage_key] = agent
            state.setdefault("stage_agents", {})[stage_key] = agent
            clear_same_stage_pending_approval(state, stage_key)
            append_log(task_dir, {"event": "artifact_finalization", "stage": stage_key, "pass": pass_number, "attempt": attempt_number, "provider": agent, "artifact_hash": result.get("final_artifact_hash"), "run_id": state.get("run_id")})
            return EXIT_SUCCESS
        failure_class = result.get("failure_class") or final["validation"].get("failure_class")
        preserve_failed(task_dir, stage_key, raw_output, failure_class or FAILURE_CLASS_MALFORMED_ARTIFACT, {"agent": agent, "metadata_path": result.get("metadata_path")})
        if failure_class in (FAILURE_CLASS_USAGE_LIMIT, FAILURE_CLASS_SOURCE_FAILURE) or (failure_class == FAILURE_CLASS_RATE_LIMIT and credible_reset(result)):
            mark_unavailable(
                state, agent, failure_class, result.get("reset_at"),
                cooldown_write=lambda a, r, rt: record_cross_task_cooldown(config, state, a, r, rt),
            )
            append_log(task_dir, {"event": "real_agent_unavailable", "agent": agent, "failure_class": failure_class, "run_id": state["run_id"]})
            next_attempt_kind = "provider_fallback"
            next_retry_reason = "provider fallback"
            continue
        human_approved_retry_key = stage_key + "_human_approved_retry"
        if failure_class == FAILURE_CLASS_MAX_TURNS and state["attempts"].get(human_approved_retry_key):
            block_transition(task_dir, state, stage_key, "real stage failed: " + failure_class, failure_class)
            return EXIT_BLOCKED
        if failure_class == FAILURE_CLASS_MAX_TURNS and useful_partial(output, CONTRACTS[stage_key]) and not state["attempts"].get(stage_key + "_completion_retry"):
            state["attempts"][stage_key + "_completion_retry"] = 1
            completion_attempt = increment_attempt(state, stage_key)
            increment_agent_count(state, agent)
            append_log(task_dir, {"event": "retry_scheduled", "stage": stage_key, "pass": pass_number, "attempt": completion_attempt, "provider": agent, "classification": FAILURE_CLASS_MAX_TURNS, "retry_reason": "max-turn completion retry", "run_id": state.get("run_id")})
            completion = invoke_stage(task_dir, state, config, stage_key, execution_mode, agent, pass_number, completion_for=output, attempt_number=completion_attempt, attempt_kind="completion_only_retry", retry_reason="max-turn completion retry", extra_context=extra_context)
            state.setdefault("real_stage_runs", {}).setdefault(stage_key, []).append(completion)
            completion_raw_output = read_candidate(completion)
            completion_output = normalize_stage_output(stage_key, completion_raw_output)
            completion_final = atomic_finalize(task_dir, stage_key, completion_output, read_only=True)
            if completion_final["finalized"]:
                completion["finalized"] = True
                completion["final_artifact_path"] = completion_final["path"]
                completion["final_artifact_hash"] = sha256_file(Path(completion_final["path"]))
                if stage_key == "05":
                    completion["dirty_baseline"] = state.get("dirty_baseline")
                assignments[stage_key] = agent
                state.setdefault("stage_agents", {})[stage_key] = agent
                clear_same_stage_pending_approval(state, stage_key)
                append_log(task_dir, {"event": "artifact_finalization", "stage": stage_key, "pass": pass_number, "attempt": completion_attempt, "provider": agent, "artifact_hash": completion.get("final_artifact_hash"), "run_id": state.get("run_id")})
                return EXIT_SUCCESS
            state["attempts"][human_approved_retry_key] = 1
            require_retry_approval(
                state,
                stage_key,
                failure_class,
                agent,
                "max-turn completion retry did not finalize",
                retry_type="human_approved_full_stage_retry",
                failed_attempt_metadata_path=result.get("metadata_path"),
                failed_attempt_number=result.get("attempt_number"),
                completion_retry_metadata_path=completion.get("metadata_path"),
                completion_retry_attempt_number=completion.get("attempt_number"),
            )
            return EXIT_BLOCKED
        if failure_class == FAILURE_CLASS_MAX_TURNS:
            state["attempts"][human_approved_retry_key] = 1
            require_retry_approval(
                state,
                stage_key,
                failure_class,
                agent,
                "unusable max-turn output",
                retry_type="human_approved_full_stage_retry",
                failed_attempt_metadata_path=result.get("metadata_path"),
                failed_attempt_number=result.get("attempt_number"),
            )
            return EXIT_BLOCKED
        if failure_class in (FAILURE_CLASS_MALFORMED_ARTIFACT, FAILURE_CLASS_EMPTY_OUTPUT, FAILURE_CLASS_TIMEOUT) and attempts < attempt_budget:
            next_attempt_kind = "transient_retry"
            next_retry_reason = "transient timeout" if failure_class == FAILURE_CLASS_TIMEOUT else "malformed output"
            append_log(task_dir, {"event": "retry_scheduled", "stage": stage_key, "pass": pass_number, "attempt": attempts + 1, "provider": agent, "classification": failure_class, "retry_reason": next_retry_reason, "run_id": state.get("run_id")})
            continue
        if failure_class == FAILURE_CLASS_RATE_LIMIT:
            block_transition(task_dir, state, stage_key, "rate limit without credible reset time", failure_class)
            return EXIT_BLOCKED
        block_transition(task_dir, state, stage_key, "real stage failed: " + (failure_class or final["validation"]["reason"]), failure_class or final["validation"].get("failure_class"))
        return EXIT_BLOCKED
    block_transition(task_dir, state, stage_key, "attempt budget exhausted", FAILURE_CLASS_MALFORMED_ARTIFACT)
    return EXIT_BLOCKED


def invoke_stage(task_dir, state, config, stage_key, execution_mode, agent, pass_number, completion_for=None, attempt_number=1, attempt_kind="normal", retry_reason="initial/no-retry", extra_context=None):
    prompt_path = render_prompt(task_dir, state["task"], stage_key, pass_number)
    if extra_context is not None:
        with open(str(prompt_path), "a", encoding="utf-8") as handle:
            handle.write("\n")
            handle.write(str(extra_context).rstrip())
            handle.write("\n")
    if completion_for is not None:
        with open(str(prompt_path), "a", encoding="utf-8") as handle:
            handle.write("\nComplete the preserved partial artifact below. Return only the complete required artifact.\n\n")
            handle.write(completion_for)
    safe_agent = "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in str(agent))
    candidate_path = orchestrator_dir(task_dir) / "runs" / ("%s-pass-%s-attempt-%s-%s-%s.candidate.md" % (stage_key, pass_number, attempt_number, safe_agent, state["run_id"]))
    before = source_snapshot() if execution_mode == "read-only" else None
    ledger_path = usage_ledger_path() if config.get("usage_ledger", {}).get("enabled", True) else None
    capture_reasoning = config.get("reasoning_capture", {}).get("enabled", True)
    result = invoke_agent(
        task_dir, config, agent, stage_key, execution_mode, prompt_path, candidate_path, state["run_id"],
        pass_number, attempt_number, attempt_kind, retry_reason, task=state["task"], ledger_path=ledger_path,
        capture_reasoning=capture_reasoning,
    )
    result["_source_before"] = before
    return result


def choose_real_agent(stage_key, state, config, assignments, execution_mode):
    safety_mode = config.get("default_safety_mode", "strict")
    unavailable = set(state.get("run_unavailable_agents") or {})
    role_key = stage_key
    independent_from = config.get("roles", {}).get(role_key, {}).get("independent_from")
    cooldowns = load_cross_task_cooldowns(config)
    for candidate in reorder_by_cooldown(configured_candidates(config, role_key), cooldowns):
        detail = agent_config(config, candidate)
        if not detail.get("enabled", True) or candidate in unavailable:
            continue
        if execution_mode == "workspace-write" and not detail.get("workspace_write"):
            continue
        if independent_from:
            prior = assignments.get(independent_from) or state.get("stage_agents", {}).get(independent_from)
            if prior == candidate:
                if safety_mode == "continuity" and config.get("allow_degraded_same_agent_review"):
                    return {"agent": candidate, "degraded": True, "fallback": candidate != config["roles"][role_key]["primary"], "reason": "degraded_same_agent_review"}
                continue
        return {
            "agent": candidate,
            "degraded": False,
            "fallback": candidate != config["roles"][role_key]["primary"],
            "cross_task_cooldown_deferred": candidate in cooldowns,
        }
    return None


def reorder_by_cooldown(candidates, cooldowns):
    """Stable partition: candidates currently on a cross-task cooldown are
    deprioritized (moved after non-cooling candidates) rather than dropped,
    so a stage always still has somewhere to go even if every configured
    candidate happens to be cooling down."""
    clean = [candidate for candidate in candidates if candidate not in cooldowns]
    cooling = [candidate for candidate in candidates if candidate in cooldowns]
    return clean + cooling


def load_cross_task_cooldowns(config):
    if not config.get("cross_task_cooldowns", {}).get("enabled", True):
        return {}
    try:
        return usage.load_cooldowns(cooldown_store_path())
    except Exception:
        return {}


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


def clamp_completed_prefix(state, completed_through):
    completed = []
    for key in STAGE_ORDER:
        completed.append(key)
        if key == completed_through:
            break
    state["completed_stages"] = completed
    state["current_stage"] = next_stage_after(completed_through)


def next_stage_after(stage_key):
    try:
        index = STAGE_ORDER.index(stage_key)
    except ValueError:
        return stage_key
    if index + 1 >= len(STAGE_ORDER):
        return None
    return STAGE_ORDER[index + 1]


def block_transition(task_dir, state, stage_key, reason, failure_class, completed_through=None):
    if completed_through is not None:
        clamp_completed_prefix(state, completed_through)
    state["current_stage"] = stage_key
    block(state, stage_key, reason, failure_class)
    append_log(task_dir, {"event": "blocked_transition", "stage": stage_key, "classification": failure_class, "reason": reason, "run_id": state.get("run_id")})
    if stage_key == "05":
        append_log(task_dir, {"event": "stage5_ambiguity_block", "stage": "05", "classification": failure_class, "reason": reason, "run_id": state.get("run_id")})


def stage5_report_provenance(task_dir, state):
    report_path = task_dir / CONTRACTS["05"].filename
    validation = validate_file(report_path, "05", read_only=True)
    if not validation["valid"]:
        return {"valid": False, "reason": "Stage 5 report is structurally invalid: " + validation["reason"], "failure_class": validation.get("failure_class", FAILURE_CLASS_MALFORMED_ARTIFACT)}
    report_hash = sha256_file(report_path)
    runs = state.get("real_stage_runs", {}).get("05") or []
    for run in reversed(runs):
        if not stage5_run_matches_report(run, report_path, report_hash, state):
            continue
        return {"valid": True, "run": run, "report_hash": report_hash}
    return {"valid": False, "reason": "Stage 5 report exists but no matching successful real Stage 5 provenance record was found", "failure_class": FAILURE_CLASS_STAGE5_AMBIGUITY}


def stage5_run_matches_report(run, report_path, report_hash, state):
    if not isinstance(run, dict):
        return False
    required = ("candidate_artifact_path", "run_id", "pass_number", "attempt_number", "attempt_kind", "retry_reason", "agent", "execution_mode")
    for key in required:
        if run.get(key) in (None, ""):
            return False
    if run.get("execution_mode") != "workspace-write":
        return False
    if run.get("exit_code") not in (0, None):
        return False
    if run.get("failure_class") not in (None, FAILURE_CLASS_MAX_TURNS, FAILURE_CLASS_UNKNOWN_FAILURE):
        return False
    candidate = Path(run.get("candidate_artifact_path"))
    if not candidate.exists() or not candidate.is_file():
        return False
    try:
        candidate_hash = sha256_file(candidate)
    except Exception:
        return False
    if candidate_hash != report_hash:
        return False
    if run.get("final_artifact_hash") and run.get("final_artifact_hash") != report_hash:
        return False
    run["final_artifact_hash"] = report_hash
    run["final_artifact_path"] = str(report_path)
    if not run.get("metadata_path") or not Path(run.get("metadata_path")).exists():
        return False
    if not run.get("stdout_path") or not Path(run.get("stdout_path")).exists():
        return False
    if not run.get("stderr_path") or not Path(run.get("stderr_path")).exists():
        return False
    if not run.get("dirty_baseline") and not state.get("dirty_baseline"):
        return False
    if not run.get("dirty_baseline"):
        run["dirty_baseline"] = state.get("dirty_baseline")
    return True


def stage5_postprocessing_complete(task_dir, state):
    report = stage5_report_provenance(task_dir, state)
    if not report["valid"]:
        return report
    manifest_path = task_dir / "05_implementation_manifest.json"
    if not manifest_path.exists():
        return {"valid": False, "reason": "Stage 5 manifest is missing", "failure_class": FAILURE_CLASS_STAGE5_AMBIGUITY}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        validate_manifest(manifest)
    except Exception as exc:
        return {"valid": False, "reason": "Stage 5 manifest is invalid: " + str(exc), "failure_class": FAILURE_CLASS_STAGE5_AMBIGUITY}
    manifest_run = manifest.get("stage5_run") or {}
    if manifest.get("stage") != "05":
        return {"valid": False, "reason": "Stage 5 manifest has wrong stage", "failure_class": FAILURE_CLASS_STAGE5_AMBIGUITY}
    if manifest_run.get("run_id") != report["run"].get("run_id"):
        return {"valid": False, "reason": "Stage 5 manifest run id does not match current Stage 5 artifact", "failure_class": FAILURE_CLASS_STAGE5_AMBIGUITY}
    if manifest_run.get("candidate_artifact_path") != report["run"].get("candidate_artifact_path"):
        return {"valid": False, "reason": "Stage 5 manifest candidate path does not match current Stage 5 artifact", "failure_class": FAILURE_CLASS_STAGE5_AMBIGUITY}
    if manifest_run.get("final_artifact_hash") != report["report_hash"]:
        return {"valid": False, "reason": "Stage 5 manifest artifact hash does not match current Stage 5 report", "failure_class": FAILURE_CLASS_STAGE5_AMBIGUITY}
    overseer = state.get("overseer") or {}
    required_paths = {
        "json_path": task_dir / "05_supervisor_handoff.json",
        "markdown_path": task_dir / "05_supervisor_handoff.md",
        "legacy_path": task_dir / "handoff.md",
    }
    for key, expected in required_paths.items():
        recorded = overseer.get(key)
        if not recorded:
            return {"valid": False, "reason": "Stage 5 handoff state is missing " + key, "failure_class": FAILURE_CLASS_STAGE5_AMBIGUITY}
        if Path(recorded) != expected or not expected.exists():
            return {"valid": False, "reason": "Stage 5 handoff path is missing or inconsistent: " + key, "failure_class": FAILURE_CLASS_STAGE5_AMBIGUITY}
    try:
        parse_overseer_candidate(json.loads(required_paths["json_path"].read_text(encoding="utf-8")))
    except Exception as exc:
        return {"valid": False, "reason": "Stage 5 supervisor handoff JSON is invalid: " + str(exc), "failure_class": FAILURE_CLASS_STAGE5_AMBIGUITY}
    if state.get("state") != "awaiting_human_test":
        return {"valid": False, "reason": "State is not awaiting human Stage 6 testing", "failure_class": FAILURE_CLASS_STAGE5_AMBIGUITY}
    if state.get("current_stage") != "06":
        return {"valid": False, "reason": "Current stage is not 06", "failure_class": FAILURE_CLASS_STAGE5_AMBIGUITY}
    checkpoint = state.get("human_checkpoint")
    if not isinstance(checkpoint, dict) or checkpoint.get("stage") != "06":
        return {"valid": False, "reason": "Human checkpoint for Stage 6 is missing", "failure_class": FAILURE_CLASS_STAGE5_AMBIGUITY}
    return {"valid": True, "run": report["run"], "manifest": manifest}


def any_stage5_postprocessing_present(task_dir, state):
    if state.get("manifest") or state.get("overseer") or state.get("human_checkpoint"):
        return True
    for name in ("05_implementation_manifest.json", "05_supervisor_handoff.json", "05_supervisor_handoff.md", "handoff.md"):
        if (task_dir / name).exists():
            return True
    return False


def checkpoint_noop_eligible(task_dir, state):
    if state.get("state") != "awaiting_human_test" or state.get("current_stage") != "06":
        return {"eligible": False, "reason": "not at human checkpoint"}
    post = stage5_postprocessing_complete(task_dir, state)
    if not post["valid"]:
        return {"eligible": False, "reason": post["reason"]}
    stage06_validation = validate_file(task_dir / CONTRACTS["06"].filename, "06", read_only=True)
    if stage06_validation["valid"]:
        return {"eligible": False, "reason": "Stage 6 manual test notes are ready; resuming to drive Stage 7/8"}
    checkpoint = state.get("human_checkpoint") or {}
    recorded = checkpoint.get("noop_hashes")
    if not isinstance(recorded, dict):
        return {"eligible": False, "reason": "checkpoint hash set is missing"}
    current = checkpoint_hashes(task_dir, state)
    if current != recorded:
        return {"eligible": False, "reason": "checkpoint hash set changed"}
    return {"eligible": True, "reason": "unchanged human checkpoint"}


def checkpoint_hashes(task_dir, state):
    paths = []
    for key in ("00", "01", "02", "03", "04", "04_gate", "05"):
        paths.append(task_dir / CONTRACTS[key].filename)
    for name in ("05_implementation_manifest.json", "05_supervisor_handoff.json", "05_supervisor_handoff.md", "handoff.md"):
        paths.append(task_dir / name)
    stage5 = last_stage_result(state, "05")
    if stage5 and stage5.get("candidate_artifact_path"):
        candidate = Path(stage5["candidate_artifact_path"])
        final_report = task_dir / CONTRACTS["05"].filename
        try:
            if candidate.resolve() != final_report.resolve():
                paths.append(candidate)
        except Exception:
            paths.append(candidate)
    result = {}
    for path in paths:
        label = str(path)
        if not path.exists() or not path.is_file():
            result[label] = None
        else:
            result[label] = sha256_file(path)
    return result


def run_overseer_or_fallback(task_dir, state, config, manifest, assignments, verification_report=None):
    handoff = None
    source = "fallback"
    for agent in configured_candidates(config, "overseer"):
        detail = agent_config(config, agent)
        if not detail.get("enabled", True):
            continue
        try:
            attempt_number = increment_attempt(state, "overseer")
            increment_agent_count(state, agent)
            append_log(task_dir, {"event": "overseer_dispatch", "stage": "overseer", "pass": 1, "attempt": attempt_number, "provider": agent, "run_id": state.get("run_id")})
            result = invoke_stage(task_dir, state, config, "overseer", "read-only", agent, 1, attempt_number=attempt_number, attempt_kind="overseer", retry_reason="initial/no-retry")
            state.setdefault("real_stage_runs", {}).setdefault("overseer", []).append(result)
            text = read_candidate(result)
            parsed = parse_overseer_candidate(text)
            handoff = parsed
            source = agent
            state["overseer"] = {"agent": agent, "status": "generated", "result": result}
            break
        except Exception as exc:
            preserve_failed(task_dir, "05", str(exc), FAILURE_CLASS_MALFORMED_OVERSEER, {"agent": agent})
            state["overseer"] = {"agent": agent, "status": "fallback", "reason": str(exc)}
            continue
    if handoff is None:
        handoff = fallback_handoff(manifest, state.get("overseer", {}).get("reason", "overseer unavailable"), verification_report)
        state.setdefault("real_stage_runs", {}).setdefault("overseer", []).append({
            "stage": "overseer",
            "run_id": state.get("run_id"),
            "pass_number": 1,
            "attempt_number": increment_attempt(state, "overseer"),
            "attempt_kind": "deterministic_fallback",
            "retry_reason": "overseer unavailable",
            "provider": "controller",
            "real_process_invoked": False,
            "candidate_artifact_path": None,
            "stdout_path": None,
            "stderr_path": None,
            "metadata_path": None,
        })
        append_log(task_dir, {"event": "deterministic_handoff_fallback", "stage": "overseer", "run_id": state.get("run_id")})

    auto_verified_eligible = (
        config.get("enable_auto_verified", True)
        and verification_report is not None
        and verification_report.get("overall_status") == "passed"
        and verification_report.get("driven_project_verified") is True
        and (verification_report.get("test_coverage_delta_signal") or {}).get("status") != "flagged"
    )
    if auto_verified_eligible and handoff.get("route") not in ("blocked", "administrator_action"):
        handoff = upgrade_to_auto_verified(handoff, verification_report)

    paths = write_handoff_files(task_dir, handoff, source)
    state["overseer"] = dict(state.get("overseer") or {}, **paths)
    return handoff


def render_auto_stage06_notes(verification_report):
    lines = [CONTRACTS["06"].heading, "", "## Automated verification summary", ""]
    for check in (verification_report or {}).get("checks", []):
        lines.append("- %s: %s" % (check.get("name"), check.get("status")))
    signal = (verification_report or {}).get("test_coverage_delta_signal") or {}
    lines.append("- test_coverage_delta_signal: %s" % signal.get("status", "unknown"))
    lines.extend([
        "",
        "This checkpoint was completed automatically: Stage 5's verification evidence",
        "(05_verification_report.md) passed with no flagged coverage gaps, and no",
        "human played the mod in-game for this task. Edit this file and change the",
        "checkbox below if manual/in-game testing is still wanted before Stage 7",
        "review runs.",
        "",
        "## Decision",
        "",
        "- [x] Accept",
        "- [ ] Reject",
        "- [ ] Needs follow-up",
    ])
    return "\n".join(lines).rstrip() + "\n"


_DECISION_RANK = {"accept": 0, "needs_followup": 1, "reject": 2}


def worse_decision(a, b):
    return a if _DECISION_RANK.get(a, 2) >= _DECISION_RANK.get(b, 2) else b


def last_nonempty_line(text):
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return None


def render_stage08_decision(final_decision, stage06_outcome, stage07_verdict):
    boxes = []
    for key, label in (("accept", "Accept"), ("reject", "Reject"), ("needs_followup", "Needs follow-up")):
        mark = "x" if key == final_decision else " "
        boxes.append("- [%s] %s" % (mark, label))
    follow_up = (
        "None -- accepted automatically."
        if final_decision == "accept"
        else "See Stage 7 diff review's \"Required fixes\" section before merging."
    )
    lines = [
        CONTRACTS["08"].heading,
        "",
        "## Decision",
        "",
    ]
    lines.extend(boxes)
    lines.extend([
        "",
        "## Reason",
        "",
        "Derived automatically from the Stage 6 manual test result (%s) and the Stage 7" % stage06_outcome,
        "diff review verdict (%s). See 06_manual_test_notes.md and 07_diff_review.md" % stage07_verdict,
        "for full detail.",
        "",
        "## Follow-up task, if needed",
        "",
        follow_up,
    ])
    return "\n".join(lines).rstrip() + "\n"


def ensure_stage08_decision(task_dir, state):
    if "08" not in state.get("completed_stages", []):
        stage06_text = (task_dir / CONTRACTS["06"].filename).read_text(encoding="utf-8")
        stage07_text = (task_dir / CONTRACTS["07"].filename).read_text(encoding="utf-8")
        stage06_outcome = manual_test_decision(stage06_text) or "needs_followup"
        stage07_verdict = last_nonempty_line(stage07_text) or "needs_followup"
        final_decision = worse_decision(stage06_outcome, stage07_verdict)
        content = render_stage08_decision(final_decision, stage06_outcome, stage07_verdict)
        result = atomic_finalize(task_dir, "08", content)
        if not result["finalized"]:
            block_transition(task_dir, state, "08", result["validation"]["reason"], result["validation"].get("failure_class"), completed_through="07")
            return EXIT_BLOCKED, None
        append_log(task_dir, {
            "event": "stage8_decision_synthesized",
            "stage": "08",
            "decision": final_decision,
            "stage06_outcome": stage06_outcome,
            "stage07_verdict": stage07_verdict,
            "run_id": state.get("run_id"),
        })
    stage08_text = (task_dir / CONTRACTS["08"].filename).read_text(encoding="utf-8")
    final_decision = manual_test_decision(stage08_text) or "needs_followup"
    return EXIT_SUCCESS, final_decision


def source_snapshot():
    return "\n".join(git_status(REPO_ROOT))

def normalize_stage_output(stage_key, output):
    """Remove harmless provider commentary before a required artifact heading."""
    contract = CONTRACTS.get(stage_key)
    if not contract:
        return output

    stripped = output.lstrip()
    headings = [contract.heading]
    if contract.legacy_heading:
        headings.append(contract.legacy_heading)

    for heading in headings:
        if stripped.startswith(heading):
            return stripped

        marker = "\n" + heading
        heading_index = stripped.find(marker)

        if heading_index >= 0:
            return stripped[heading_index + 1:]

    return output

def read_candidate(result):
    path = result.get("candidate_artifact_path")
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def credible_reset(result):
    return bool(result.get("reset_at"))


def last_stage_result(state, stage_key):
    runs = state.get("real_stage_runs", {}).get(stage_key) or []
    return runs[-1] if runs else None


def run_scenario(task_dir, task, state, scenario):
    ensure_seed_artifacts(task_dir)
    assignments = current_assignments(task_dir)
    reconcile_artifacts(task_dir, state, read_only=False)
    mock = MockAgent(scenario)
    while state["current_stage"]:
        stage_key = state["current_stage"]
        if stage_key == "06" and scenario.get("stop_for_human_test"):
            state["state"] = "awaiting_human_test"
            state["human_checkpoint"] = {"stage": "06", "created_at": now(), "reason": "manual test notes required"}
            return EXIT_BLOCKED
        if stage_key == "08" and scenario.get("stop_for_final_decision"):
            state["state"] = "awaiting_final_decision"
            return EXIT_BLOCKED
        code = run_stage(task_dir, state, scenario, mock, stage_key, assignments)
        reconcile_artifacts(task_dir, state, read_only=False)
        if code != EXIT_SUCCESS:
            return code
    state["state"] = "complete"
    state["last_failure"] = None
    return EXIT_SUCCESS


def ensure_seed_artifacts(task_dir):
    task_dir.mkdir(parents=True, exist_ok=True)
    for stage_key in ("00", "01"):
        path = task_dir / CONTRACTS[stage_key].filename
        if not path.exists():
            result = atomic_finalize(task_dir, stage_key, valid_artifact(stage_key))
            if not result["finalized"]:
                raise ControllerError("failed to seed " + stage_key, EXIT_VALIDATION)


def run_stage(task_dir, state, scenario, mock, stage_key, assignments):
    route = choose_agent(stage_key, state, scenario, assignments)
    if not route:
        block(state, stage_key, "no valid fallback satisfies role and safety rules", FAILURE_CLASS_SOURCE_FAILURE)
        return EXIT_BLOCKED
    agent = route["agent"]
    if route.get("fallback") or route.get("degraded"):
        event = {
            "stage": stage_key,
            "agent": agent,
            "reason": route.get("reason") or "fallback",
            "timestamp": now(),
        }
        state["fallback_events"].append(event)
        append_log(task_dir, {"event": "fallback", "details": event, "run_id": state["run_id"]})
    if stage_key in ("00", "01", "06", "08"):
        result = atomic_finalize(task_dir, stage_key, valid_artifact(stage_key))
        if result["finalized"]:
            assignments[stage_key] = agent
            return EXIT_SUCCESS
        block(state, stage_key, result["validation"]["reason"], result["validation"].get("failure_class"))
        return EXIT_VALIDATION
    if state.get("state") == "awaiting_retry_approval":
        if consume_approved_retry_if_present(state, stage_key):
            append_log(task_dir, {"event": "approval_consumed", "stage": stage_key, "run_id": state["run_id"]})
            state["state"] = "running"
        else:
            return EXIT_BLOCKED
    attempt = increment_attempt(state, stage_key)
    increment_agent_count(state, agent)
    response = mock.invoke(agent, stage_key, attempt)
    write_trace(task_dir, state, stage_key, agent, attempt, response)
    failure = response.get("failure_class")
    if not failure:
        result = atomic_finalize(task_dir, stage_key, response["output"])
        if result["finalized"]:
            assignments[stage_key] = agent
            if state.get("pending_approval") and state["pending_approval"].get("stage") == stage_key:
                state["pending_approval"] = None
            return EXIT_SUCCESS
        return handle_failure(task_dir, state, scenario, mock, stage_key, agent, attempt, result["validation"]["failure_class"], result["failed_path"], response["output"], assignments)
    if failure in (FAILURE_CLASS_USAGE_LIMIT,):
        mark_unavailable(state, agent, failure, response.get("reset_at"))
        append_log(task_dir, {"event": "agent_unavailable", "agent": agent, "failure_class": failure, "run_id": state["run_id"]})
        return EXIT_SUCCESS
    if failure == FAILURE_CLASS_RATE_LIMIT and response.get("reset_at"):
        mark_unavailable(state, agent, failure, response.get("reset_at"))
        append_log(task_dir, {"event": "agent_unavailable", "agent": agent, "failure_class": failure, "run_id": state["run_id"]})
        return EXIT_SUCCESS
    if failure == FAILURE_CLASS_MAX_TURNS:
        result = atomic_finalize(task_dir, stage_key, response["output"])
        if result["finalized"]:
            assignments[stage_key] = agent
            if state.get("pending_approval") and state["pending_approval"].get("stage") == stage_key:
                state["pending_approval"] = None
            return EXIT_SUCCESS
        preserve_failed(task_dir, stage_key, response["output"], FAILURE_CLASS_MAX_TURNS, {"agent": agent})
        if useful_partial(response["output"], CONTRACTS[stage_key]) and not state["attempts"].get(stage_key + "_completion_retry"):
            state["attempts"][stage_key + "_completion_retry"] = 1
            increment_agent_count(state, agent)
            completion = mock.invoke(agent, stage_key, attempt + 1, completion_only=True)
            final = atomic_finalize(task_dir, stage_key, completion["output"])
            if final["finalized"]:
                assignments[stage_key] = agent
                return EXIT_SUCCESS
            block(state, stage_key, final["validation"]["reason"], FAILURE_CLASS_MALFORMED_ARTIFACT)
            return EXIT_BLOCKED
        require_retry_approval(state, stage_key, failure, agent, "unusable max-turn output")
        return EXIT_BLOCKED
    preserve_failed(task_dir, stage_key, response.get("output", ""), failure, {"agent": agent})
    return handle_failure(task_dir, state, scenario, mock, stage_key, agent, attempt, failure, None, response.get("output", ""), assignments, response=response)


def handle_failure(task_dir, state, scenario, mock, stage_key, agent, attempt, failure, failed_path, output, assignments, response=None):
    response = response or {}
    if failure in (FAILURE_CLASS_MALFORMED_ARTIFACT, FAILURE_CLASS_EMPTY_OUTPUT):
        if attempt < int(scenario.get("attempt_budget", 2)):
            return EXIT_SUCCESS
        block(state, stage_key, "attempt budget exhausted after " + failure, failure)
        return EXIT_BLOCKED
    if failure == FAILURE_CLASS_TIMEOUT:
        if attempt < int(scenario.get("attempt_budget", 2)):
            return EXIT_SUCCESS
        block(state, stage_key, "timeout retry budget exhausted", failure)
        return EXIT_BLOCKED
    if failure == FAILURE_CLASS_RATE_LIMIT:
        block(state, stage_key, "rate limit without credible reset time", failure)
        return EXIT_BLOCKED
    if failure == FAILURE_CLASS_SANDBOX_ENVIRONMENT and scenario.get("fallback_same_safety"):
        mark_unavailable(state, agent, failure, None)
        return EXIT_SUCCESS
    if failure == FAILURE_CLASS_PROCESS_INTERRUPTED:
        state["state"] = "ready"
        state["last_failure"] = failure_record(stage_key, failure, "process interrupted; partial output preserved")
        return EXIT_INTERRUPTED
    if failure in (FAILURE_CLASS_PERMISSION_ERROR, FAILURE_CLASS_SANDBOX_ENVIRONMENT, FAILURE_CLASS_SOURCE_FAILURE, FAILURE_CLASS_UNKNOWN_FAILURE):
        block(state, stage_key, failure + " blocked", failure)
        return EXIT_BLOCKED
    block(state, stage_key, "unhandled failure", failure)
    return EXIT_BLOCKED


def require_retry_approval(
    state,
    stage_key,
    failure_class,
    agent,
    reason,
    retry_type="temporary_full_retry",
    failed_attempt_metadata_path=None,
    failed_attempt_number=None,
    completion_retry_metadata_path=None,
    completion_retry_attempt_number=None,
):
    state["state"] = "awaiting_retry_approval"
    pending = {
        "approval_id": "retry-" + uuid.uuid4().hex[:12],
        "stage": stage_key,
        "failure_class": failure_class,
        "retry_type": retry_type,
        "agent": agent,
        "created_at": now(),
        "reason": reason,
        "approved": False,
        "consumed": False,
    }
    for key, value in (
        ("failed_attempt_metadata_path", failed_attempt_metadata_path),
        ("failed_attempt_number", failed_attempt_number),
        ("completion_retry_metadata_path", completion_retry_metadata_path),
        ("completion_retry_attempt_number", completion_retry_attempt_number),
    ):
        if value is not None:
            pending[key] = value
    state["pending_approval"] = pending
    state["last_failure"] = failure_record(stage_key, failure_class, reason)


def consume_approved_retry_if_present(state, stage_key):
    pending = state.get("pending_approval")
    if pending and pending.get("stage") == stage_key and pending.get("approved") and not pending.get("consumed"):
        pending["consumed"] = True
        pending["consumed_at"] = now()
        return True
    return False


def clear_same_stage_pending_approval(state, stage_key):
    pending = state.get("pending_approval")
    if pending and pending.get("stage") == stage_key:
        state["pending_approval"] = None


def block(state, stage_key, reason, failure_class):
    state["state"] = "blocked"
    state["last_failure"] = failure_record(stage_key, failure_class, reason)


def failure_record(stage_key, failure_class, reason):
    return {"stage": stage_key, "failure_class": failure_class, "reason": reason, "timestamp": now()}


def increment_attempt(state, stage_key):
    attempts = state.setdefault("attempts", {})
    attempts[stage_key] = int(attempts.get(stage_key, 0)) + 1
    return attempts[stage_key]


def increment_agent_count(state, agent):
    counts = state.setdefault("agent_call_counts", {})
    counts[agent] = int(counts.get(agent, 0)) + 1


def mark_unavailable(state, agent, reason, reset_at, cooldown_write=None):
    state.setdefault("run_unavailable_agents", {})[agent] = {
        "reason": reason,
        "reset_at": reset_at,
        "recorded_at": now(),
        "run_id": state.get("run_id"),
    }
    if cooldown_write is not None:
        cooldown_write(agent, reason, reset_at)


def record_cross_task_cooldown(config, state, agent, reason, reset_at):
    cfg = config.get("cross_task_cooldowns", {})
    if not cfg.get("enabled", True) or reason not in (FAILURE_CLASS_USAGE_LIMIT, FAILURE_CLASS_RATE_LIMIT):
        return
    usage.record_cooldown(
        cooldown_store_path(), agent, reason, reset_at,
        state.get("task"), state.get("run_id"),
        int(cfg.get("default_cooldown_seconds", 900)),
    )


def begin_new_run(state):
    unavailable = state.get("run_unavailable_agents") or {}
    for agent, detail in unavailable.items():
        history = dict(detail)
        history["agent"] = agent
        state.setdefault("unavailability_history", []).append(history)
    state["run_unavailable_agents"] = {}
    if state.get("state") in ("blocked", "failed", "awaiting_human_test", "awaiting_final_decision"):
        state["state"] = "ready"


def current_assignments(task_dir):
    assignments = {}
    root = orchestrator_dir(task_dir) / "traces"
    if not root.exists():
        return assignments
    for trace in sorted(root.glob("*.json")):
        try:
            data = json.loads(trace.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("final_candidate"):
            assignments[data.get("stage")] = data.get("agent")
    return assignments


def write_trace(task_dir, state, stage_key, agent, attempt, response):
    traces = orchestrator_dir(task_dir) / "traces"
    traces.mkdir(parents=True, exist_ok=True)
    path = traces / ("%s-attempt-%d-%s.json" % (stage_key, attempt, agent))
    payload = {
        "stage": stage_key,
        "agent": agent,
        "attempt": attempt,
        "failure_class": response.get("failure_class"),
        "final_candidate": response.get("failure_class") is None,
        "run_id": state.get("run_id"),
        "timestamp": now(),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def make_run_id():
    return "run-" + uuid.uuid4().hex


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def mock_test():
    scenarios = load_scenarios()
    run_root = FIXTURES_ROOT / "_mock_runs"
    run_root.mkdir(parents=True, exist_ok=True)
    suite_root = run_root / ("suite-" + uuid.uuid4().hex[:12])
    suite_root.mkdir()
    original_tasks_root = TASKS_ROOT
    passed = 0
    failed = []
    try:
        globals()["TASKS_ROOT"] = suite_root
        for name, scenario in sorted(scenarios.items()):
            task = "fixture-" + name
            code = mock_run(task, name)
            expected = int(scenario.get("expected_exit", EXIT_SUCCESS))
            try:
                assert code == expected, "exit %s != %s" % (code, expected)
                state = load_state(task_dir_for(task), task)
                expected_state = scenario.get("expected_state")
                if expected_state:
                    assert state["state"] == expected_state, "state %s != %s" % (state["state"], expected_state)
                expected_counts = scenario.get("expected_agent_call_counts")
                if expected_counts:
                    assert state["agent_call_counts"] == expected_counts, "counts %s != %s" % (state["agent_call_counts"], expected_counts)
                if scenario.get("expect_failed_output"):
                    failed_dir = orchestrator_dir(task_dir_for(task)) / "failed"
                    assert failed_dir.exists() and list(failed_dir.iterdir()), "missing preserved failed output"
                passed += 1
            except AssertionError as exc:
                failed.append("%s: %s" % (name, exc))
        approval_result = exercise_approval_workflow(suite_root)
        if approval_result is None:
            passed += 1
        else:
            failed.append("approval_workflow: " + approval_result)
        lock_result = exercise_unlock_workflow(suite_root)
        if lock_result is None:
            passed += 1
        else:
            failed.append("unlock_workflow: " + lock_result)
        resume_result = exercise_resume_reconciliation(suite_root)
        if resume_result is None:
            passed += 1
        else:
            failed.append("resume_reconciliation: " + resume_result)
    finally:
        globals()["TASKS_ROOT"] = original_tasks_root
        shutil.rmtree(str(suite_root), ignore_errors=True)
    if failed:
        print("mock tests failed:")
        for item in failed:
            print(" - " + item)
        return EXIT_VALIDATION
    print("mock tests passed: %d" % passed)
    return EXIT_SUCCESS


def exercise_approval_workflow(root):
    globals()["TASKS_ROOT"] = root
    task = "approval-flow"
    code = mock_run(task, "max_turns_unusable")
    if code != EXIT_BLOCKED:
        return "initial run did not block"
    state = load_state(task_dir_for(task), task)
    approval_id = state["pending_approval"]["approval_id"]
    if approve_retry(task, "wrong-id") != EXIT_BAD_INPUT:
        return "mismatch was not rejected"
    if approve_retry(task, approval_id) != EXIT_SUCCESS:
        return "valid approval was not accepted"
    scenario = dict(load_scenarios()["max_turns_unusable"])
    scenario["actions"] = {"02": "success"}
    run_id = make_run_id()
    task_dir = task_dir_for(task)
    with TaskLock(task_dir, "approval-consume", run_id):
        state = load_state(task_dir, task)
        state["run_id"] = run_id
        if not consume_approved_retry_if_present(state, "02"):
            return "approval was not consumable"
        write_state_atomic(task_dir, state)
    if approve_retry(task, approval_id) != EXIT_BAD_INPUT:
        return "consumed approval was not rejected"
    return None


def exercise_unlock_workflow(root):
    globals()["TASKS_ROOT"] = root
    task = "unlock-flow"
    task_dir = task_dir_for(task)
    task_dir.mkdir(parents=True, exist_ok=True)
    lock_root = orchestrator_dir(task_dir)
    lock_root.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": 99999999,
        "host": socket.gethostname(),
        "started_at": now(),
        "command": "mock",
        "run_id": "stale",
    }
    (lock_root / "lock.json").write_text(json.dumps(payload) + "\n", encoding="utf-8")
    if mock_run(task, "success") != EXIT_LOCKED:
        return "stale lock did not block"
    if unlock(task, "test unlock") != EXIT_SUCCESS:
        return "unlock command failed"
    return None


def exercise_resume_reconciliation(root):
    globals()["TASKS_ROOT"] = root
    task = "resume-reconcile"
    task_dir = task_dir_for(task)
    task_dir.mkdir(parents=True, exist_ok=True)
    for stage_key in ("00", "01", "02"):
        result = atomic_finalize(task_dir, stage_key, valid_artifact(stage_key))
        if not result["finalized"]:
            return "failed to prepare finalized artifact " + stage_key
    state = new_state(task, "old-run")
    state["completed_stages"] = ["00", "01"]
    write_state_atomic(task_dir, state)
    if mock_run(task, "success") != EXIT_SUCCESS:
        return "resume run failed"
    state = load_state(task_dir, task)
    if state["agent_call_counts"].get("codex") != 3:
        return "stage 02 was invoked instead of reconciled"
    if "02" not in state["completed_stages"]:
        return "stage 02 was not marked completed"
    return None
