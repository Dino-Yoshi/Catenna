"""Deterministic mock pipeline controller."""

from __future__ import print_function

import calendar
import copy
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from .artifacts import CONTRACTS, manual_test_decision, sha256_file, useful_partial, validate_file, validate_text
from . import attempts as attempts_module
from . import color
from . import config
from . import cost_policy
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
from .locking import ExecutionOwnership, LockError, TaskLock, explicit_unlock, validate_execution_ownership
from .mock_agent import MockAgent, valid_artifact
from .manifest import capture_dirty_baseline, changed_files_since, git_status, write_manifest
from .overseer import fallback_handoff, parse_overseer_candidate, upgrade_to_auto_verified, write_handoff_files
from .policies import choose_agent
from .prompts import render_prompt
from .real_runner import ManagedProcessInterrupted, invoke_agent
from .durable import DurableStorageError
from .runner import atomic_finalize, preserve_failed
from .source_identity import SourceIdentityError, capture_source_identity, changed_identity_paths, format_identity, same_identity, with_identity_change
from .state import CorruptState, STAGE_ORDER, acknowledge_consumed_inputs, append_log, load_state, new_state, orchestrator_dir, reconcile_artifacts, write_state_atomic
from . import decision as decision_module
from . import evidence as evidence_module
from . import gates as gates_module
from . import integrity as integrity_module
from . import report as report_module
from . import stage5 as stage5_module
from . import tail as tail_module
from . import usage
from . import verification


REPO_ROOT = Path.cwd()
TASKS_ROOT = REPO_ROOT / ".agent-pipeline" / "tasks"
# Mock scenario fixtures describe this controller's own state machine, not
# anything about the project being driven, so they live with the package
# (independent of which project's directory is REPO_ROOT/cwd) rather than
# under the driven project's .agent-pipeline/.
PACKAGE_ROOT = Path(__file__).resolve().parent
FIXTURES_ROOT = PACKAGE_ROOT / "fixtures"
SCENARIO_PATH = FIXTURES_ROOT / "mock_scenarios.json"
USAGE_ROOT = REPO_ROOT / ".agent-pipeline" / "usage"
BACKGROUND_RUN_ID_ENV = "CATENNA_BACKGROUND_RUN_ID"
BACKGROUND_ADOPTION_TOKEN_ENV = "CATENNA_BACKGROUND_ADOPTION_TOKEN"


def usage_ledger_path():
    return USAGE_ROOT / "ledger.jsonl"


def outcomes_ledger_path():
    return USAGE_ROOT / "outcomes.jsonl"


def usage_ledger_enabled(config):
    return config.get("usage_ledger", {}).get("enabled", True)


def cooldown_store_path():
    return USAGE_ROOT / "agent_cooldowns.json"


def execution_identity():
    """Use a background launch reservation when present, else a fresh run."""
    run_id = os.environ.pop(BACKGROUND_RUN_ID_ENV, None)
    adoption_token = os.environ.pop(BACKGROUND_ADOPTION_TOKEN_ENV, None)
    if run_id and adoption_token:
        return run_id, adoption_token
    return make_run_id(), None


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


def pipeline_init(force=False, codex_model=None):
    tasks_existed = TASKS_ROOT.exists()
    usage_existed = USAGE_ROOT.exists()
    config_existed = config.CONFIG_PATH.exists()

    TASKS_ROOT.mkdir(parents=True, exist_ok=True)
    USAGE_ROOT.mkdir(parents=True, exist_ok=True)
    config.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    default_config = copy.deepcopy(config.DEFAULT_CONFIG)
    if codex_model:
        default_config["agents"]["codex"]["model"] = codex_model
    default_text = json.dumps(default_config, indent=2, sort_keys=True) + "\n"
    writing_defaults = not config_existed or force
    if not config_existed:
        config.CONFIG_PATH.write_text(default_text, encoding="utf-8")
        config_status = "created"
    elif force:
        config.CONFIG_PATH.write_text(default_text, encoding="utf-8")
        config_status = "overwritten with defaults"
    else:
        config_status = "exists, unchanged"

    print("%s: %s" % (config.CONFIG_PATH, config_status))
    print("%s: %s" % (TASKS_ROOT, "exists" if tasks_existed else "created"))
    print("%s: %s" % (USAGE_ROOT, "exists" if usage_existed else "created"))

    if writing_defaults:
        if codex_model:
            print(
                "codex model set to %r, but pricing.%s is not configured -- "
                "codex estimated-cost accounting will still show cost_estimated=unknown "
                "until you add pricing.codex.%s rates to %s"
                % (codex_model, codex_model, codex_model, config.CONFIG_PATH)
            )
        else:
            print(
                "warning: agents.codex.model and pricing.codex are unset -- "
                "codex stages will show cost_estimated=unknown until you pass "
                "--codex-model or edit %s directly" % config.CONFIG_PATH
            )

    config.load_config()
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


def _cooldown_expired(reset_at):
    """True only if reset_at parses and is in the past. Missing/unparseable
    reset_at is treated conservatively as still-blocking (False), matching
    usage._compute_expires_at's own fallback of assuming a cooldown is
    still live rather than assuming it already lapsed."""
    if not reset_at:
        return False
    try:
        expires_at = calendar.timegm(time.strptime(str(reset_at), "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return False
    return expires_at <= time.time()


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
        if state["state"] == "blocked" and all(
            _cooldown_expired(detail.get("reset_at")) for detail in state["run_unavailable_agents"].values()
        ):
            print("status_note: displayed state is stale -- all recorded agent cooldowns have expired; rerun to refresh")
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
    current = print_current_evidence(task_dir, state)
    if "08" in state.get("completed_stages", []):
        decision_path = task_dir / CONTRACTS["08"].filename
        if decision_path.exists():
            final_decision = manual_test_decision(decision_path.read_text(encoding="utf-8"))
            print("final_decision: %s" % (final_decision or "unknown"))
            if current is not None and current["stage08"]["current"]:
                print("final_decision_status: current")
            else:
                print("final_decision_status: historical (%s)" % (current["stage08"]["reason"] if current else "evidence unavailable"))
    return EXIT_SUCCESS


def current_evidence_summary(task_dir, state):
    """Read-only C06 view of current eligibility; None if config is unusable."""
    try:
        cfg = load_config()
    except ConfigError:
        return None
    identity, error = current_source_identity(task_dir)
    summary = evidence_module.evaluate(task_dir, state, cfg, identity, error)
    summary["review_attempts"] = current_review_attempt_summary(task_dir, state, cfg, identity)
    return summary


def current_review_attempt_summary(task_dir, state, config, identity=None, inputs=None):
    if identity is None:
        identity, error = current_source_identity(task_dir)
        if error is not None:
            return {"identity": None, "attempts_used": 0, "attempts_allowed": int(config.get("stage_attempt_budget", 2)), "reason": error}
    result = evidence_module.review_input_identity(
        task_dir, state, identity,
        inputs if inputs is not None else evidence_module.current_inputs(task_dir, config),
        config=config,
    )
    review_identity = result.get("identity")
    return {
        "identity": review_identity,
        "attempts_used": attempts_module.count_stage7_attempts(task_dir, review_identity),
        "attempts_allowed": int(config.get("stage_attempt_budget", 2)),
        "reason": result.get("reason"),
    }


def print_current_evidence(task_dir, state):
    summary = current_evidence_summary(task_dir, state)
    if summary is None:
        print("source_identity: unavailable (invalid config)")
        print("current_acceptance: no (evidence cannot be evaluated with an invalid config)")
        return None
    identity = summary["source_identity"]
    print("source_identity: %s" % (format_identity(identity) if identity else "unavailable (%s)" % summary["source_error"]))
    for key, label in (("verification", "verification"), ("stage06", "stage06_evidence"), ("stage07", "stage07_evidence"), ("stage08", "stage08_evidence")):
        detail = summary[key]
        print("%s: %s (%s)" % (label, "current" if detail["current"] else "not current", detail["reason"]))
    review = summary["review_attempts"]
    print("review_input_identity: %s" % (review.get("identity") or "unavailable (%s)" % review.get("reason")))
    print("review_attempts: %s/%s" % (review["attempts_used"], review["attempts_allowed"]))
    print("current_acceptance: %s (%s)" % (
        color.green("yes") if summary["current_acceptance"] else color.red("no"),
        summary["reason"],
    ))
    return summary


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
    print_current_evidence(task_dir, state)
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
        run_id, adoption_token = execution_identity()
        with ExecutionOwnership(task_dir, REPO_ROOT, "verify", run_id, task, adoption_token=adoption_token):
            state = load_state(task_dir, task)
            try:
                report, record, error = run_bound_verification(task_dir, state, config, run_build=run_build, origin="verify", run_id=run_id)
            except ManagedProcessInterrupted:
                print("verification interrupted")
                return EXIT_INTERRUPTED
            if error is not None:
                print(error)
                return EXIT_LOCKED
    except LockError as exc:
        print(str(exc))
        return EXIT_LOCKED
    except CorruptState as exc:
        print("CORRUPT state for %s: %s" % (task, exc))
        return EXIT_VALIDATION
    def pass_fail_color(status_value):
        return color.green(status_value) if status_value == "passed" else color.red(status_value)

    print("task: %s" % task)
    print("overall_status: %s" % pass_fail_color(report["overall_status"]))
    after = record.get("source_after")
    print("source_identity: %s" % (format_identity(after) if after else "unavailable"))
    print("source_identity_current: %s (%s)" % (
        color.green("true") if record.get("source_current") else color.red("false"),
        record.get("source_reason"),
    ))
    print("driven_project_checks_configured: %s (%d)" % (
        str(bool(report.get("driven_project_checks_configured"))).lower(),
        int(report.get("driven_project_check_count") or 0),
    ))
    print("driven_project_verified: %s (%s)" % (
        str(bool(report.get("driven_project_verified"))).lower(),
        report.get("driven_project_verification_reason", "unknown"),
    ))
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
    # A pass whose source changed during checks does not qualify as current.
    return EXIT_SUCCESS if report["overall_status"] == "passed" and record.get("source_current") else EXIT_VALIDATION


def launch_background(task, argv_tail, log_name):
    task_dir = task_dir_for(task)
    orch = task_dir / ".orchestrator"
    orch.mkdir(parents=True, exist_ok=True)
    log_path = orch / log_name
    argv = [sys.executable, "-m", "agent_pipeline.cli"] + list(argv_tail)
    run_id = make_run_id()
    adoption_token = uuid.uuid4().hex
    command = "background-" + str(argv_tail[0])
    env = os.environ.copy()
    env[BACKGROUND_RUN_ID_ENV] = run_id
    env[BACKGROUND_ADOPTION_TOKEN_ENV] = adoption_token
    try:
        with ExecutionOwnership(task_dir, REPO_ROOT, command, run_id, task, adoption_token=adoption_token) as ownership:
            log_handle = open(str(log_path), "a", encoding="utf-8")
            try:
                proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=log_handle,
                    cwd=str(REPO_ROOT),
                    env=env,
                    start_new_session=True,
                )
                ownership.transfer_to(proc.pid)
            finally:
                log_handle.close()
    except LockError as exc:
        print(str(exc))
        return EXIT_LOCKED
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
        cost = format_cost(bucket)
        estimated_cost = format_estimated_cost(bucket)
        print("  %s: calls=%d failures=%d duration=%.1fs %s %s %s %s" % (name, bucket["count"], bucket["failures"], bucket["duration_seconds"], tokens, cost, estimated_cost, format_cache_hit(bucket)))
    overall = summary["overall"]
    overall_tokens = "in=%d out=%d" % (overall["input_tokens"], overall["output_tokens"]) if overall["tokens_known"] else "tokens=unknown"
    print("overall: calls=%d failures=%d duration=%.1fs %s %s %s %s" % (overall["count"], overall["failures"], overall["duration_seconds"], overall_tokens, format_cost(overall), format_estimated_cost(overall), format_cache_hit(overall)))
    if "codex" in summary["groups"]:
        try:
            config = load_config()
        except ConfigError:
            config = None
        if config is not None and not config.get("agents", {}).get("codex", {}).get("model"):
            print(
                "warning: agents.codex.model is unset - codex estimated-cost tracking and model "
                "attribution are disabled (codex falls back to its own CLI default, which "
                "this pipeline cannot see or record). Set agents.codex.model and a matching "
                "pricing.codex rate table in orchestrator.json to fix this."
            )
    try:
        cooldowns = usage.load_cooldowns(cooldown_store_path())
        if cooldowns:
            print("cross_task_cooldowns: " + json.dumps(cooldowns, sort_keys=True))
    except Exception:
        pass
    return EXIT_SUCCESS


def format_cost(bucket):
    return ("cost=$%.4f" % bucket["total_cost_usd"]) if bucket["cost_known"] else "cost=unknown"


def format_estimated_cost(bucket):
    return ("cost_estimated=$%.4f" % bucket["total_cost_usd_estimated"]) if bucket.get("cost_estimated_known") else "cost_estimated=unknown"


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
    report = report_module.generate_report(task_dir, task, state, usage_entries=entries, evidence=current_evidence_summary(task_dir, state))
    print(report_module.render_markdown(report), end="")
    return EXIT_SUCCESS


def unlock(task, reason):
    task_dir = task_dir_for(task)
    result = explicit_unlock(task_dir, reason, repo_root=REPO_ROOT)
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
            if pending.get("retry_type") == "failed_write_source_change":
                try:
                    pending["approved_source_baseline"] = capture_writer_source_baseline(task_dir)
                except Exception as exc:
                    print("cannot approve retry because the current source baseline is unavailable: %s" % exc)
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
    run_id, adoption_token = execution_identity()
    task_dir = task_dir_for(task)
    try:
        config = load_config()
    except ConfigError as exc:
        print("invalid real-run config: %s" % exc)
        return EXIT_VALIDATION
    try:
        with ExecutionOwnership(task_dir, REPO_ROOT, "run", run_id, task, adoption_token=adoption_token):
            state = load_state(task_dir, task)
            noop = checkpoint_noop_eligible(task_dir, state)
            if noop["eligible"]:
                print("pipeline-run %s: %s" % (task, state["state"]))
                return EXIT_BLOCKED
            state["run_id"] = run_id
            begin_new_run(state)
            append_log(task_dir, {"event": "real_run_started", "run_id": run_id})
            try:
                code = run_real_pipeline(task_dir, task, state, config, allow_dirty)
            except ManagedProcessInterrupted as exc:
                persist_interrupted_invocation(task_dir, state, exc)
                code = EXIT_INTERRUPTED
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
    except DurableStorageError as exc:
        print("BLOCKED durable storage failure for %s: %s" % (task, exc))
        return EXIT_BLOCKED


def run_real_pipeline(task_dir, task, state, config, allow_dirty):
    task_dir.mkdir(parents=True, exist_ok=True)
    for seed_stage in ("00", "01"):
        validation = validate_file(task_dir / CONTRACTS[seed_stage].filename, seed_stage, read_only=True)
        if not validation["valid"]:
            state["completed_stages"] = []
            block_transition(task_dir, state, seed_stage, "Stage %s is missing or invalid: %s" % (seed_stage, validation["reason"]), validation.get("failure_class"))
            return EXIT_BLOCKED

    recovery_error = attempts_module.recover(
        task_dir, state, atomic_finalize, stages=STAGE_ORDER,
        failed_writer_recovery=lambda dispatch, completed: recover_failed_writer_approval(
            task_dir, state, dispatch, completed
        ),
    )
    if recovery_error:
        if state.get("state") != "awaiting_retry_approval":
            block_transition(task_dir, state, state.get("current_stage") or "02", "durable recovery blocked: " + recovery_error, FAILURE_CLASS_STAGE5_AMBIGUITY)
        return EXIT_BLOCKED
    reconcile_artifacts(task_dir, state, read_only=False)
    cost_control = config.get("cost_control", {})
    if cost_control.get("enabled", False):
        ledger_enabled = usage_ledger_enabled(config)
        if ledger_enabled:
            ledger_entries = usage.read_entries(usage_ledger_path())
        else:
            ledger_entries = []
        quality_entries = []
        if (
            cost_control.get("quality_aware", False)
            and ledger_enabled
            and "04" in cost_control.get("eligible_stages", [])
        ):
            quality_entries = usage.read_entries(outcomes_ledger_path())
        overrides = cost_policy.compute_stage_overrides(config, ledger_entries, quality_entries=quality_entries)
        state["stage_overrides"] = overrides
        append_log(task_dir, {"event": "cost_policy_applied", "overrides": overrides, "run_id": state.get("run_id")})
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
        baseline = state.get("dirty_baseline")
        if not baseline:
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
        if not stage5_current_run and not post_check["valid"]:
            # Adopt: report_check above already independently verified this
            # report corresponds to a real, successful, workspace-write Stage 5
            # run (stage5_report_provenance). Rebuild missing or partial
            # controller-owned postprocessing from that immutable authority;
            # never repeat the implementation call merely because bookkeeping
            # was interrupted.
            append_log(task_dir, {"event": "stage5_postprocessing_adopted", "stage": "05", "provenance_run_id": report_check["run"].get("run_id"), "run_id": state.get("run_id")})

        try:
            manifest = write_manifest(task_dir, REPO_ROOT, state, report_check["run"], baseline)
            report_check["run"]["dirty_changed_files"] = manifest.get("changed_files", [])
            append_log(task_dir, {"event": "manifest_generation", "stage": "05", "run_id": state.get("run_id"), "path": str(task_dir / "05_implementation_manifest.json")})
        except Exception as exc:
            block_transition(task_dir, state, "05", "Stage 5 manifest is structurally invalid: " + str(exc), FAILURE_CLASS_MALFORMED_ARTIFACT, completed_through="04_gate")
            return EXIT_BLOCKED

        code = run_stage6_transition(task_dir, state, config, assignments, manifest)
        if code != EXIT_SUCCESS:
            return code

    # C06: Stage 6-8 evidence counts only while bound to the current source
    # identity and consumed inputs. Continuation and acceptance revalidate it.
    code = ensure_current_stage6(task_dir, state, config, assignments, baseline)
    if code != EXIT_SUCCESS:
        return code
    # Idempotent: covers the human-checkpoint accept route, where
    # 06_manual_test_notes.md is written directly with no atomic_finalize
    # call, so the auto_verified branch's acknowledgement never runs.
    acknowledge_consumed_inputs(task_dir, state, "06")
    code = ensure_current_review(task_dir, state, config, assignments)
    if code != EXIT_SUCCESS:
        return code
    reconcile_artifacts(task_dir, state, read_only=False)

    code, final_decision = ensure_current_decision(task_dir, state, config)
    if code != EXIT_SUCCESS:
        return code
    acknowledge_consumed_inputs(task_dir, state, "08")
    reconcile_artifacts(task_dir, state, read_only=False)
    state["last_failure"] = None
    return EXIT_SUCCESS if final_decision == "accept" else EXIT_VALIDATION


def run_stage6_transition(task_dir, state, config, assignments, manifest):
    """Verify, obtain the handoff, then complete Stage 6 automatically or
    stop at the human checkpoint."""
    verification_report, record, error = run_bound_verification(task_dir, state, config)
    if error is not None:
        append_log(task_dir, {"event": "verification_error", "stage": "05", "reason": error, "run_id": state.get("run_id")})
        if record.get("ownership_failure"):
            block_transition(
                task_dir,
                state,
                "05",
                "execution ownership postcondition failed after verification check: " + error,
                FAILURE_CLASS_SANDBOX_ENVIRONMENT,
            )
            return EXIT_BLOCKED

    handoff = run_overseer_or_fallback(task_dir, state, config, manifest, assignments, verification_report)
    if handoff is None:
        return EXIT_BLOCKED

    eligibility = stage6_automatic_eligibility(task_dir, state, config, verification_report, handoff)
    if handoff.get("route") == "auto_verified":
        if eligibility["eligible"]:
            identity = eligibility["identity"]
            result = atomic_finalize(task_dir, "06", render_auto_stage06_notes(verification_report, identity))
            if result["finalized"]:
                record_stage06_evidence(task_dir, state, config, identity, "auto", verification_record_id=eligibility["verification_record"]["record_id"])
                acknowledge_consumed_inputs(task_dir, state, "06")
                append_log(task_dir, {"event": "stage6_auto_verified", "stage": "06", "source_identity": format_identity(identity), "run_id": state.get("run_id")})
                reconcile_artifacts(task_dir, state, read_only=False)
                return EXIT_SUCCESS
        else:
            append_log(task_dir, {"event": "stage6_auto_verification_rejected", "stage": "06", "reason": eligibility["reason"], "run_id": state.get("run_id")})

    identity, _error = current_source_identity(task_dir)
    return stage6_human_checkpoint(task_dir, state, identity, eligibility["reason"])


def stage6_human_checkpoint(task_dir, state, identity, reason):
    cited = format_identity(identity) if identity else None
    state["state"] = "awaiting_human_test"
    state["current_stage"] = "06"
    state["human_checkpoint"] = {
        "stage": "06",
        "created_at": now(),
        "reason": reason,
        "source_identity": cited,
        "noop_hashes": checkpoint_hashes(task_dir, state),
    }
    if cited:
        state["next_required_human_action"] = (
            "Run manual Stage 6 testing against source identity %s and record 06_manual_test_notes.md "
            "citing that identity in a `## Source identity` section (see `catenna status`)." % cited
        )
    else:
        state["next_required_human_action"] = "Source identity is unavailable; resolve it (see `catenna status`) before recording Stage 6 notes."
    state["last_failure"] = None
    append_log(task_dir, {"event": "human_checkpoint_transition", "stage": "06", "reason": reason, "source_identity": cited, "run_id": state.get("run_id")})
    return EXIT_BLOCKED


def controller_owned_paths(task_dir=None):
    paths = [TASKS_ROOT, USAGE_ROOT, config.CONFIG_PATH.parent]
    if task_dir is not None:
        paths.append(task_dir)
    return paths


def current_source_identity(task_dir=None):
    """Return (identity, error). Any capture failure leaves evidence unverified."""
    try:
        return capture_source_identity(REPO_ROOT, controller_owned_paths(task_dir)), None
    except SourceIdentityError as exc:
        return None, str(exc)


def run_bound_verification(task_dir, state, config, run_build=False, origin="pipeline", run_id=None):
    """Run verification and append a source-bound evidence record.

    The attempt is persisted before checks start, so an interrupted or
    failed verification supersedes older passing evidence for the same
    inputs. Returns (report, record, error)."""
    run_id = run_id or state.get("run_id")
    inputs = evidence_module.subset(evidence_module.current_inputs(task_dir, config, run_build=run_build), evidence_module.VERIFICATION_INPUT_KEYS)
    before, before_error = current_source_identity(task_dir)
    record = evidence_module.append(state, "verification", {
        "status": "running",
        "origin": origin,
        "run_id": run_id,
        "inputs": inputs,
        "source_before": before,
        "source_before_error": before_error,
        "source_current": False,
    })
    write_state_atomic(task_dir, state)
    verification_settings = config.get("verification", {})
    report = None
    error = None
    ownership_failure = False
    try:
        report = verification.run_verification(
            task_dir,
            REPO_ROOT,
            run_build=run_build,
            allow_pid=os.getpid(),
            driven_project_commands=verification_settings.get("driven_project_commands", []),
            skip_self_check=verification_settings.get("skip_self_check", False),
            build_implies_compile=verification_settings.get("build_implies_compile", False),
            run_id=run_id,
            source_excludes=controller_owned_paths(task_dir),
        )
    except verification.VerificationError as exc:
        error = str(exc)
        ownership_failure = isinstance(exc, verification.ExecutionOwnershipError)
    except ManagedProcessInterrupted as exc:
        report = getattr(exc, "report", None)
        record["status"] = "interrupted"
        record["source_reason"] = "verification was interrupted"
        record["source_current"] = False
        report_path = task_dir / "05_verification_report.json"
        if report is not None and report_path.is_file():
            record["report_hash"] = sha256_file(report_path)
        state["current_stage"] = "05"
        block(state, "05", "verification was interrupted", FAILURE_CLASS_PROCESS_INTERRUPTED)
        write_state_atomic(task_dir, state)
        append_log(task_dir, {"event": "verification_evidence_recorded", "status": "interrupted", "source_current": False, "reason": "verification was interrupted", "origin": origin, "run_id": run_id})
        raise
    except BaseException:
        record["status"] = "interrupted"
        record["source_reason"] = "verification was interrupted"
        write_state_atomic(task_dir, state)
        raise
    after, after_error = current_source_identity(task_dir)
    if error is not None:
        status_value = "error"
    elif isinstance(report, dict) and report.get("overall_status") in ("passed", "failed", "incomplete"):
        status_value = report["overall_status"]
    else:
        status_value = "malformed"
    current, reason = verification_source_binding(before, after, before_error, after_error, report)
    record.update({
        "status": status_value,
        "error": error,
        "ownership_failure": ownership_failure,
        "source_after": after,
        "source_after_error": after_error,
        "source_current": current,
        "source_reason": reason,
    })
    report_path = task_dir / "05_verification_report.json"
    if report is not None and report_path.is_file():
        record["report_hash"] = sha256_file(report_path)
    write_state_atomic(task_dir, state)
    append_log(task_dir, {"event": "verification_evidence_recorded", "status": status_value, "source_current": current, "reason": reason, "origin": origin, "run_id": run_id})
    return report, record, error


def verification_source_binding(before, after, before_error, after_error, report):
    if before_error or after_error:
        return False, "source identity unavailable: " + (before_error or after_error)
    if not same_identity(before, after):
        return False, with_identity_change("source identity changed during verification", before, after)
    own = report.get("source_identity") if isinstance(report, dict) else None
    if own is not None:
        # The report's own inner before/after capture must agree as well.
        if not isinstance(own, dict) or not own.get("current"):
            return False, (own or {}).get("reason") if isinstance(own, dict) else "verification report source identity is malformed"
        if not same_identity(own.get("after"), after):
            return False, "verification report source identity does not match the controller's capture"
    return True, "source identity unchanged across verification"


def stage6_automatic_eligibility(task_dir, state, config, verification_report, handoff):
    """C01 evidence predicate plus C06 current-evidence checks, evaluated at
    the transition itself."""
    eligibility = automatic_verification_eligibility(config, verification_report, handoff)
    if not eligibility["eligible"]:
        return eligibility
    identity, error = current_source_identity(task_dir)
    if error is not None:
        return {"eligible": False, "reason": "source identity is unavailable; automatic Stage 6 completion requires current evidence: " + error}
    inputs = evidence_module.current_inputs(task_dir, config)
    check = evidence_module.verification_status(state, identity, inputs)
    if not check["current"]:
        return {"eligible": False, "reason": "verification evidence is not current: " + check["reason"]}
    return {"eligible": True, "reason": eligibility["reason"], "identity": identity, "verification_record": check["record"]}


def record_stage06_evidence(task_dir, state, config, identity, route, verification_record_id=None):
    keys = evidence_module.VERIFICATION_INPUT_KEYS if route == "auto" else evidence_module.MANUAL_STAGE06_INPUT_KEYS
    return evidence_module.append(state, "stage06", {
        "status": "bound",
        "route": route,
        "artifact_hash": evidence_module.artifact_hash(task_dir, "06"),
        "source": identity,
        "inputs": evidence_module.subset(evidence_module.current_inputs(task_dir, config), keys),
        "verification_record_id": verification_record_id,
        "run_id": state.get("run_id"),
    })


def invalidate_evidence(task_dir, state, stage_keys, reason, extra_files=(), keep_in_place=()):
    """Archive stale Stage 6-8 results into history and remove the
    controller/agent-generated ones so they must be produced again.
    Human-authored files named in keep_in_place are archived, not removed."""
    filenames = [CONTRACTS[key].filename for key in stage_keys] + list(extra_files)
    archived, directory = evidence_module.archive(task_dir, filenames, reason, state.get("run_id"))
    # The invalidation sidecar is the durable authority recovery consults.
    # Record it before deleting any canonical artifact.
    attempts_module.invalidate_promoted(task_dir, stage_keys, reason, directory)
    evidence_module.remove(task_dir, [name for name in archived if name not in keep_in_place and name not in extra_files])
    for key in stage_keys:
        kind = "stage" + key
        last = evidence_module.latest(state, kind)
        if last is not None and last.get("status") != "invalidated":
            entry = {"status": "invalidated", "reason": reason, "history_path": str(directory) if directory else None, "run_id": state.get("run_id")}
            if key == "07":
                entry["review_input_identity"] = last.get("review_input_identity")
            evidence_module.append(state, kind, entry)
    append_log(task_dir, {"event": "evidence_invalidated", "stages": list(stage_keys), "reason": reason, "archived": archived, "history_path": str(directory) if directory else None, "run_id": state.get("run_id")})
    return archived


def ensure_current_stage6(task_dir, state, config, assignments, baseline):
    identity, error = current_source_identity(task_dir)
    if error is not None:
        block_transition(task_dir, state, "06", "source identity is unavailable; Stage 6-8 evidence remains unverified: " + error, FAILURE_CLASS_SOURCE_FAILURE, completed_through="05")
        return EXIT_BLOCKED
    inputs = evidence_module.current_inputs(task_dir, config)
    status_value = evidence_module.stage06_status(task_dir, state, identity, inputs, config=config)
    if status_value["current"]:
        return EXIT_SUCCESS
    record = status_value.get("record") or {}
    if status_value["binding"] == "stale" and record.get("route") == "auto":
        return reverify_after_drift(task_dir, state, config, assignments, baseline, status_value["reason"])
    if status_value["binding"] == "unbound":
        problem = evidence_module.manual_citation_problem(task_dir, state, identity)
        if problem is None:
            verification_check = evidence_module.manual_acceptance_verification_status(
                task_dir, state, config, identity, inputs,
            )
            if not verification_check["current"]:
                return stage6_human_checkpoint(task_dir, state, identity, verification_check["reason"])
            bound = record_stage06_evidence(task_dir, state, config, identity, "manual")
            append_log(task_dir, {"event": "stage6_manual_evidence_bound", "stage": "06", "source_identity": format_identity(identity), "record_id": bound["record_id"], "run_id": state.get("run_id")})
            return EXIT_SUCCESS
        reason = problem
        stale_stages = ["07", "08"]
    else:
        reason = status_value["reason"]
        stale_stages = ["06", "07", "08"]
    invalidate_evidence(task_dir, state, stale_stages, reason, keep_in_place=(CONTRACTS["06"].filename,))
    clamp_completed_prefix(state, "05")
    return stage6_human_checkpoint(task_dir, state, identity, reason)


def reverify_after_drift(task_dir, state, config, assignments, baseline, reason):
    """Automatic Stage 6 evidence went stale: preserve it, then re-run
    verification and the Stage 6 transition without re-running Stage 5."""
    report_check = stage5_report_provenance(task_dir, state)
    if not report_check["valid"]:
        block_transition(task_dir, state, "05", "cannot re-verify stale Stage 6 evidence: " + report_check["reason"], report_check.get("failure_class", FAILURE_CLASS_STAGE5_AMBIGUITY), completed_through="04_gate")
        return EXIT_BLOCKED
    invalidate_evidence(task_dir, state, ["06", "07", "08"], reason, extra_files=evidence_module.POSTPROCESSING_FILES)
    reconcile_artifacts(task_dir, state, read_only=False)
    append_log(task_dir, {"event": "stage6_reverification_required", "stage": "06", "reason": reason, "run_id": state.get("run_id")})
    try:
        manifest = write_manifest(task_dir, REPO_ROOT, state, report_check["run"], baseline)
    except Exception as exc:
        block_transition(task_dir, state, "05", "Stage 5 manifest is structurally invalid: " + str(exc), FAILURE_CLASS_MALFORMED_ARTIFACT, completed_through="04_gate")
        return EXIT_BLOCKED
    return run_stage6_transition(task_dir, state, config, assignments, manifest)


def ensure_current_review(task_dir, state, config, assignments):
    identity, error = current_source_identity(task_dir)
    if error is not None:
        block_transition(task_dir, state, "07", "source identity is unavailable; Stage 7 review cannot be bound: " + error, FAILURE_CLASS_SOURCE_FAILURE, completed_through="06")
        return EXIT_BLOCKED
    inputs = evidence_module.current_inputs(task_dir, config)
    review = current_review_attempt_summary(task_dir, state, config, identity, inputs)
    review_identity = review.get("identity")
    if review_identity is None:
        block_transition(task_dir, state, "07", "Stage 7 review input identity is unavailable: " + review["reason"], FAILURE_CLASS_SOURCE_FAILURE, completed_through="06")
        return EXIT_BLOCKED
    status_value = evidence_module.stage07_status(task_dir, state, identity, inputs)
    if status_value["current"]:
        return EXIT_SUCCESS
    if (task_dir / CONTRACTS["07"].filename).exists() or (task_dir / CONTRACTS["08"].filename).exists():
        invalidate_evidence(task_dir, state, ["07", "08"], "Stage 7 review is not current: " + status_value["reason"])
        reconcile_artifacts(task_dir, state, read_only=False)
    runs_before = len((state.get("real_stage_runs") or {}).get("07") or [])
    code = ensure_real_stage(
        task_dir, state, config, "07", "read-only", assignments,
        pass_number=1, review_input_identity=review_identity,
    )
    runs_after = (state.get("real_stage_runs") or {}).get("07") or []
    if len(runs_after) == runs_before:
        return code
    after, after_error = current_source_identity(task_dir)
    stage06 = evidence_module.latest(state, "stage06") or {}
    entry = {
        "run_id": state.get("run_id"),
        "attempt_number": runs_after[-1].get("attempt_number"),
        "stage06_record_id": stage06.get("record_id"),
        "source_before": identity,
        "source_after": after,
        "source_after_error": after_error,
        "inputs": evidence_module.subset(inputs, evidence_module.REVIEW_INPUT_KEYS),
        "review_input_identity": review_identity,
    }
    if code != EXIT_SUCCESS:
        entry["status"] = "failed"
        evidence_module.append(state, "stage07", entry)
        return code
    entry["artifact_hash"] = evidence_module.artifact_hash(task_dir, "07")
    if after_error is not None or not same_identity(identity, after):
        entry["status"] = "unbound"
        evidence_module.append(state, "stage07", entry)
        reason = "source identity changed during Stage 7 review; the review is not bound to the current source"
        if after_error is None:
            reason = with_identity_change(reason, identity, after)
        invalidate_evidence(task_dir, state, ["07"], reason)
        block_transition(task_dir, state, "07", reason, FAILURE_CLASS_SOURCE_FAILURE, completed_through="06")
        return EXIT_BLOCKED
    entry["status"] = "passed"
    evidence_module.append(state, "stage07", entry)
    return EXIT_SUCCESS


def ensure_current_decision(task_dir, state, config):
    identity, error = current_source_identity(task_dir)
    if error is not None:
        block_transition(task_dir, state, "08", "source identity is unavailable; final decision cannot be bound: " + error, FAILURE_CLASS_SOURCE_FAILURE, completed_through="07")
        return EXIT_BLOCKED, None
    status_value = evidence_module.stage08_status(task_dir, state, identity)
    if not status_value["current"]:
        if (task_dir / CONTRACTS["08"].filename).exists():
            invalidate_evidence(task_dir, state, ["08"], "Stage 8 decision is not current: " + status_value["reason"])
            reconcile_artifacts(task_dir, state, read_only=False)
    code, final_decision = ensure_stage08_decision(task_dir, state)
    if code != EXIT_SUCCESS:
        return code, None
    if not status_value["current"]:
        evidence_module.append(state, "stage08", {
            "status": "recorded",
            "decision": final_decision,
            "artifact_hash": evidence_module.artifact_hash(task_dir, "08"),
            "stage06_record_id": (evidence_module.latest(state, "stage06") or {}).get("record_id"),
            "stage07_record_id": (evidence_module.latest(state, "stage07") or {}).get("record_id"),
            "source": identity,
            "run_id": state.get("run_id"),
        })
    # Acceptance boundary: the whole binding must still hold right now.
    summary = evidence_module.evaluate(task_dir, state, config, *current_source_identity(task_dir))
    for key in ("stage06", "stage07", "stage08"):
        if not summary[key]["current"]:
            reason = "final decision evidence is no longer current: " + summary[key]["reason"]
            block_transition(task_dir, state, "08", reason, FAILURE_CLASS_SOURCE_FAILURE)
            return EXIT_BLOCKED, None
    return EXIT_SUCCESS, final_decision


def run_stage4_gate_loop(task_dir, state, config, assignments):
    outcome_path = None
    if usage_ledger_enabled(config):
        outcome_path = outcomes_ledger_path()
    return gates_module.run_stage4_gate_loop(
        task_dir,
        state,
        config,
        assignments,
        ensure_real_stage,
        block_transition,
        outcome_ledger_path=outcome_path,
    )


def stage4_rejected_gate_context(task_dir, state, pass_number):
    return gates_module.stage4_rejected_gate_context(task_dir, state, pass_number)


def merge_matching_stage_override_into_config(config, state, stage_key, agent):
    if not config.get("cost_control", {}).get("enabled", False):
        return config
    override = ((state.get("stage_overrides") or {}).get(stage_key) or {}).get(agent)
    if not override:
        return config
    return merge_stage_override_into_config(config, stage_key, override)


def merge_stage_override_into_config(config, stage_key, override):
    updated = dict(config)
    roles = dict(config.get("roles", {}))
    role = dict(roles.get(stage_key, {}))
    if override.get("model") is not None:
        role["model_override"] = override.get("model")
    if override.get("effort") is not None:
        role["effort_override"] = override.get("effort")
    roles[stage_key] = role
    updated["roles"] = roles
    return updated


def ensure_real_stage(task_dir, state, config, stage_key, execution_mode, assignments, pass_number=1, force=False, extra_context=None, review_input_identity=None):
    recovery_error = attempts_module.recover(
        task_dir, state, atomic_finalize, stages=(stage_key,),
        failed_writer_recovery=lambda dispatch, completed: recover_failed_writer_approval(
            task_dir, state, dispatch, completed
        ),
    )
    if recovery_error:
        if state.get("state") != "awaiting_retry_approval":
            block_transition(task_dir, state, stage_key, "durable recovery blocked: " + recovery_error, FAILURE_CLASS_STAGE5_AMBIGUITY)
        return EXIT_BLOCKED
    fresh_input_stages = set(state.pop("_r01_fresh_input_stages", []) or [])
    fresh_input_allowance = stage_key in fresh_input_stages
    if fresh_input_stages - {stage_key}:
        state["_r01_fresh_input_stages"] = sorted(fresh_input_stages - {stage_key})
    pending = state.get("pending_approval") or {}
    awaiting_retry_approval = state.get("state") == "awaiting_retry_approval"
    approved_retry_dispatch = False
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
        if pending.get("retry_type") == "failed_write_source_change":
            try:
                current_source = current_source_baseline(task_dir)
            except TypeError:
                # Compatibility for adapters/tests that supplied the former
                # zero-argument source-baseline hook.
                current_source = current_source_baseline()
            if current_source is None or not same_source_baseline(current_source, pending.get("approved_source_baseline")):
                pending["approval_id"] = "retry-" + uuid.uuid4().hex[:12]
                pending["approved"] = False
                pending.pop("approved_at", None)
                pending.pop("approved_source_baseline", None)
                pending["reason"] = "source changed or became unavailable after retry approval; approve the failed writer retry again against current source"
                state["state"] = "awaiting_retry_approval"
                state["last_failure"] = failure_record(stage_key, pending.get("failure_class"), pending["reason"])
                write_state_atomic(task_dir, state)
                append_log(task_dir, {"event": "retry_approval_invalidated", "stage": stage_key, "reason": pending["reason"], "run_id": state.get("run_id")})
                return EXIT_BLOCKED
        if completed:
            consume_approved_retry_if_present(state, stage_key)
            append_log(task_dir, {"event": "approval_consumed", "stage": stage_key, "run_id": state.get("run_id")})
            state["pending_approval"] = None
            return EXIT_SUCCESS
        consume_approved_retry_if_present(state, stage_key)
        append_log(task_dir, {"event": "approval_consumed", "stage": stage_key, "run_id": state.get("run_id")})
        state["state"] = "running"
        approved_retry_dispatch = True
    attempt_budget = int(config.get("stage_attempt_budget", 2))
    source_change_retry = pending.get("retry_type") == "failed_write_source_change"
    if stage_key == "07" and review_input_identity is not None and not (approved_retry_dispatch and not source_change_retry):
        attempts_used = attempts_module.count_stage7_attempts(task_dir, review_input_identity)
    elif force or fresh_input_allowance or (approved_retry_dispatch and not source_change_retry):
        attempts_used = 0
    else:
        attempts_used = int(state.setdefault("attempts", {}).get(stage_key, 0))
    next_attempt_kind = "normal"
    next_retry_reason = "initial/no-retry"
    if stage_key == "07" and review_input_identity is not None and attempts_used >= attempt_budget:
        block_transition(
            task_dir, state, stage_key,
            "Stage 7 attempt budget exhausted for review-input identity %s; use approve-retry to authorize another attempt" % review_input_identity,
            FAILURE_CLASS_MALFORMED_ARTIFACT,
        )
        return EXIT_BLOCKED
    while attempts_used < attempt_budget:
        route = choose_real_agent(stage_key, state, config, assignments, execution_mode)
        if not route:
            block_transition(task_dir, state, stage_key, "no configured capable runner exists for required role/mode", FAILURE_CLASS_SOURCE_FAILURE)
            return EXIT_BLOCKED
        agent = route["agent"]
        dispatch_config = merge_matching_stage_override_into_config(config, state, stage_key, agent)
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
        attempt_number = increment_attempt(state, stage_key)
        attempts_used += 1
        increment_agent_count(state, agent)
        append_log(task_dir, {"event": "stage_dispatch", "stage": stage_key, "pass": pass_number, "attempt": attempt_number, "provider": agent, "attempt_kind": next_attempt_kind, "retry_reason": next_retry_reason, "run_id": state.get("run_id")})
        result = invoke_stage(task_dir, state, dispatch_config, stage_key, execution_mode, agent, pass_number, attempt_number=attempt_number, attempt_kind=next_attempt_kind, retry_reason=next_retry_reason, extra_context=extra_context, review_input_identity=review_input_identity)
        state.setdefault("real_stage_runs", {}).setdefault(stage_key, []).append(result)
        state.setdefault("execution_modes", {})[stage_key] = execution_mode
        postcondition = invocation_postcondition(task_dir, result, execution_mode)
        if not postcondition["valid"]:
            persist_attempt_completion(task_dir, state, result, read_candidate(result), {"valid": False}, False)
            block_postcondition_failure(task_dir, state, stage_key, result, agent, postcondition)
            return EXIT_BLOCKED
        raw_output = read_candidate(result)
        output = normalize_stage_output(stage_key, raw_output)
        validation = validate_text(output, CONTRACTS[stage_key], read_only=True)
        process_succeeded = successful_process_result(result)
        if not persist_attempt_completion(task_dir, state, result, output, validation, process_succeeded):
            block_transition(task_dir, state, stage_key, "durable completion persistence failed", FAILURE_CLASS_SOURCE_FAILURE)
            return EXIT_BLOCKED
        if process_succeeded and validation["valid"]:
            try:
                final = atomic_finalize(task_dir, stage_key, output, read_only=True)
            except DurableStorageError as exc:
                block_transition(task_dir, state, stage_key, "durable artifact promotion failed: " + str(exc), FAILURE_CLASS_SOURCE_FAILURE)
                return EXIT_BLOCKED
        else:
            final = {"finalized": False, "validation": validation}
        append_log(task_dir, {"event": "stage_attempt_result", "stage": stage_key, "pass": pass_number, "attempt": attempt_number, "provider": agent, "classification": result.get("failure_class") or final["validation"].get("failure_class") or "success", "finalized": bool(final.get("finalized")), "run_id": state.get("run_id")})
        if final["finalized"]:
            result["finalized"] = True
            result["final_artifact_path"] = final["path"]
            result["final_artifact_hash"] = sha256_file(Path(final["path"]))
            if stage_key == "05":
                result["dirty_baseline"] = state.get("dirty_baseline")
            try:
                attempts_module.record_promotion(task_dir, result, Path(final["path"]))
                if result.get("attempt_id"):
                    state.setdefault("durable_attempts", {}).setdefault(result["attempt_id"], {})["status"] = "promoted"
                write_state_atomic(task_dir, state)
            except DurableStorageError as exc:
                block_transition(task_dir, state, stage_key, "durable promotion bookkeeping failed: " + str(exc), FAILURE_CLASS_SOURCE_FAILURE)
                return EXIT_BLOCKED
            acknowledge_consumed_inputs(task_dir, state, stage_key)
            assignments[stage_key] = agent
            state.setdefault("stage_agents", {})[stage_key] = agent
            clear_same_stage_pending_approval(state, stage_key)
            append_log(task_dir, {"event": "artifact_finalization", "stage": stage_key, "pass": pass_number, "attempt": attempt_number, "provider": agent, "artifact_hash": result.get("final_artifact_hash"), "run_id": state.get("run_id")})
            return EXIT_SUCCESS
        failure_class = result.get("failure_class") or final["validation"].get("failure_class")
        if failure_class is None and not process_succeeded:
            failure_class = FAILURE_CLASS_UNKNOWN_FAILURE
        preserve_failed(task_dir, stage_key, raw_output, failure_class or FAILURE_CLASS_MALFORMED_ARTIFACT, {"agent": agent, "metadata_path": result.get("metadata_path")})
        if failed_writer_requires_approval(task_dir, state, stage_key, execution_mode, agent, result, failure_class):
            return EXIT_BLOCKED
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
        completion_context_available = final["validation"]["valid"] or useful_partial(output, CONTRACTS[stage_key])
        if failure_class == FAILURE_CLASS_MAX_TURNS and completion_context_available and not state["attempts"].get(stage_key + "_completion_retry"):
            state["attempts"][stage_key + "_completion_retry"] = 1
            completion_attempt = increment_attempt(state, stage_key)
            increment_agent_count(state, agent)
            append_log(task_dir, {"event": "retry_scheduled", "stage": stage_key, "pass": pass_number, "attempt": completion_attempt, "provider": agent, "classification": FAILURE_CLASS_MAX_TURNS, "retry_reason": "max-turn completion retry", "run_id": state.get("run_id")})
            completion = invoke_stage(task_dir, state, dispatch_config, stage_key, execution_mode, agent, pass_number, completion_for=output, attempt_number=completion_attempt, attempt_kind="completion_only_retry", retry_reason="max-turn completion retry", extra_context=extra_context, review_input_identity=review_input_identity)
            state.setdefault("real_stage_runs", {}).setdefault(stage_key, []).append(completion)
            completion_postcondition = invocation_postcondition(task_dir, completion, execution_mode)
            if not completion_postcondition["valid"]:
                persist_attempt_completion(task_dir, state, completion, read_candidate(completion), {"valid": False}, False)
                block_postcondition_failure(task_dir, state, stage_key, completion, agent, completion_postcondition)
                return EXIT_BLOCKED
            completion_raw_output = read_candidate(completion)
            completion_output = normalize_stage_output(stage_key, completion_raw_output)
            completion_validation = validate_text(completion_output, CONTRACTS[stage_key], read_only=True)
            completion_succeeded = successful_process_result(completion)
            if not persist_attempt_completion(task_dir, state, completion, completion_output, completion_validation, completion_succeeded):
                block_transition(task_dir, state, stage_key, "durable completion persistence failed", FAILURE_CLASS_SOURCE_FAILURE)
                return EXIT_BLOCKED
            if completion_succeeded and completion_validation["valid"]:
                try:
                    completion_final = atomic_finalize(task_dir, stage_key, completion_output, read_only=True)
                except DurableStorageError as exc:
                    block_transition(task_dir, state, stage_key, "durable artifact promotion failed: " + str(exc), FAILURE_CLASS_SOURCE_FAILURE)
                    return EXIT_BLOCKED
            else:
                completion_final = {"finalized": False, "validation": completion_validation}
            if completion_final["finalized"]:
                completion["finalized"] = True
                completion["final_artifact_path"] = completion_final["path"]
                completion["final_artifact_hash"] = sha256_file(Path(completion_final["path"]))
                if stage_key == "05":
                    completion["dirty_baseline"] = state.get("dirty_baseline")
                try:
                    attempts_module.record_promotion(task_dir, completion, Path(completion_final["path"]))
                    if completion.get("attempt_id"):
                        state.setdefault("durable_attempts", {}).setdefault(completion["attempt_id"], {})["status"] = "promoted"
                    write_state_atomic(task_dir, state)
                except DurableStorageError as exc:
                    block_transition(task_dir, state, stage_key, "durable promotion bookkeeping failed: " + str(exc), FAILURE_CLASS_SOURCE_FAILURE)
                    return EXIT_BLOCKED
                acknowledge_consumed_inputs(task_dir, state, stage_key)
                assignments[stage_key] = agent
                state.setdefault("stage_agents", {})[stage_key] = agent
                clear_same_stage_pending_approval(state, stage_key)
                append_log(task_dir, {"event": "artifact_finalization", "stage": stage_key, "pass": pass_number, "attempt": completion_attempt, "provider": agent, "artifact_hash": completion.get("final_artifact_hash"), "run_id": state.get("run_id")})
                return EXIT_SUCCESS
            completion_failure = completion.get("failure_class") or completion_validation.get("failure_class")
            if completion_failure is None and not completion_succeeded:
                completion_failure = FAILURE_CLASS_UNKNOWN_FAILURE
            preserve_failed(task_dir, stage_key, completion_raw_output, completion_failure or FAILURE_CLASS_MALFORMED_ARTIFACT, {"agent": agent, "metadata_path": completion.get("metadata_path")})
            if failed_writer_requires_approval(task_dir, state, stage_key, execution_mode, agent, completion, completion_failure):
                return EXIT_BLOCKED
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
        if failure_class in (FAILURE_CLASS_MALFORMED_ARTIFACT, FAILURE_CLASS_EMPTY_OUTPUT, FAILURE_CLASS_TIMEOUT) and attempts_used < attempt_budget:
            next_attempt_kind = "transient_retry"
            next_retry_reason = "transient timeout" if failure_class == FAILURE_CLASS_TIMEOUT else "malformed output"
            append_log(task_dir, {"event": "retry_scheduled", "stage": stage_key, "pass": pass_number, "attempt": attempt_number + 1, "provider": agent, "classification": failure_class, "retry_reason": next_retry_reason, "run_id": state.get("run_id")})
            continue
        if failure_class == FAILURE_CLASS_RATE_LIMIT:
            block_transition(task_dir, state, stage_key, "rate limit without credible reset time", failure_class)
            return EXIT_BLOCKED
        block_transition(task_dir, state, stage_key, "real stage failed: " + (failure_class or final["validation"]["reason"]), failure_class or final["validation"].get("failure_class"))
        return EXIT_BLOCKED
    block_transition(task_dir, state, stage_key, "attempt budget exhausted", FAILURE_CLASS_MALFORMED_ARTIFACT)
    return EXIT_BLOCKED


def invoke_stage(task_dir, state, config, stage_key, execution_mode, agent, pass_number, completion_for=None, attempt_number=1, attempt_kind="normal", retry_reason="initial/no-retry", extra_context=None, review_input_identity=None):
    prompt_path = render_prompt(task_dir, state["task"], stage_key, pass_number, config=config)
    if extra_context is not None:
        with open(str(prompt_path), "a", encoding="utf-8") as handle:
            handle.write("\n")
            handle.write(str(extra_context).rstrip())
            handle.write("\n")
    if completion_for is not None:
        with open(str(prompt_path), "a", encoding="utf-8") as handle:
            handle.write("\nComplete the preserved partial artifact below. Return only the complete required artifact.\n\n")
            handle.write(completion_for)
    ledger_path = usage_ledger_path() if usage_ledger_enabled(config) else None
    source_before = None
    source_before_error = None
    try:
        source_before = current_source_identity(task_dir) if execution_mode == "read-only" else (capture_writer_source_baseline(task_dir), None)
    except Exception as exc:
        source_before = (None, str(exc))
    before, source_before_error = source_before
    dispatch = attempts_module.prepare_dispatch(
        task_dir, state, stage_key, agent, execution_mode, pass_number, attempt_number,
        attempt_kind, retry_reason, prompt_path, state.get("input_hashes"),
        before if execution_mode == "workspace-write" else state.get("dirty_baseline"),
        dict(agent_config(config, agent)),
        review_input_identity=review_input_identity,
    )
    attempt_dir = attempts_module.attempts_root(task_dir) / dispatch["attempt_id"]
    prompt_path = Path(dispatch["prompt_path"])
    safe_agent = "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in str(agent))
    candidate_path = orchestrator_dir(task_dir) / "runs" / (
        "%s-pass-%s-attempt-%s-%s-%s-%s.candidate.md" %
        (stage_key, pass_number, attempt_number, safe_agent, state["run_id"], dispatch["attempt_id"])
    )
    allowed_runtime_paths = invocation_runtime_paths(task_dir, candidate_path, ledger_path)
    allowed_runtime_paths.extend([attempt_dir / "dispatch.json", attempt_dir / "prompt.md", attempt_dir / "completed.json", attempt_dir / "result.md", attempt_dir / "promoted.json"])
    protected_before = None
    protected_before_error = None
    try:
        protected_before = capture_protected_integrity(task_dir, allowed_runtime_paths)
    except integrity_module.IntegrityError as exc:
        protected_before_error = str(exc)
    capture_reasoning = config.get("reasoning_capture", {}).get("enabled", True)
    interruption = None
    try:
        result = invoke_agent(
            task_dir, config, agent, stage_key, execution_mode, prompt_path, candidate_path, state["run_id"],
            pass_number, attempt_number, attempt_kind, retry_reason, task=state["task"], ledger_path=ledger_path,
            capture_reasoning=capture_reasoning, repo_root=REPO_ROOT,
        )
    except ManagedProcessInterrupted as exc:
        interruption = exc
        result = exc.result
    result["attempt_id"] = dispatch["attempt_id"]
    result["dispatch_path"] = str(attempt_dir / "dispatch.json")
    result["_source_before"] = before
    if source_before_error is not None:
        result["_source_before_error"] = source_before_error
    result["_protected_before"] = protected_before
    result["_protected_excluded_paths"] = [str(path) for path in allowed_runtime_paths]
    result["_ownership_expected"] = {
        "run_id": state.get("run_id"),
        "controller_pid": os.getpid(),
    }
    if protected_before_error is not None:
        result["_protected_before_error"] = protected_before_error
    if interruption is not None:
        interruption.result = result
        raise interruption
    return result


def persist_interrupted_invocation(task_dir, state, exc):
    """Persist an interrupted agent attempt without launching any follow-up work."""
    result = getattr(exc, "result", None)
    if isinstance(result, dict) and result.get("attempt_id"):
        stage_key = result.get("stage") or state.get("current_stage") or "02"
        runs = state.setdefault("real_stage_runs", {}).setdefault(stage_key, [])
        if not any(item.get("attempt_id") == result.get("attempt_id") for item in runs):
            runs.append(result)
        state.setdefault("execution_modes", {})[stage_key] = result.get("execution_mode")
        postcondition = result.get("postcondition")
        if postcondition is None:
            postcondition = invocation_postcondition(task_dir, result, result.get("execution_mode"))
        output = read_candidate(result)
        reason = "agent invocation was interrupted"
        failure_class = FAILURE_CLASS_PROCESS_INTERRUPTED
        if not postcondition["valid"]:
            reason = postcondition["reason"]
            failure_class = FAILURE_CLASS_SANDBOX_ENVIRONMENT
        validation = {"valid": False, "reason": reason, "failure_class": failure_class}
        persist_attempt_completion(task_dir, state, result, output, validation, False)
        preserve_failed(task_dir, stage_key, output, failure_class, {"agent": result.get("agent"), "metadata_path": result.get("metadata_path"), "reason": reason})
        state["current_stage"] = stage_key
        if postcondition["valid"]:
            block(state, stage_key, "agent invocation was interrupted by signal %s" % exc.signum, FAILURE_CLASS_PROCESS_INTERRUPTED)
        else:
            block(state, stage_key, reason, failure_class)
    elif state.get("state") != "blocked":
        stage_key = state.get("current_stage") or "02"
        state["current_stage"] = stage_key
        block(state, stage_key, "managed process was interrupted by signal %s" % exc.signum, FAILURE_CLASS_PROCESS_INTERRUPTED)
    write_state_atomic(task_dir, state)
    append_log(task_dir, {"event": "managed_process_interrupted", "stage": state.get("current_stage"), "signal": exc.signum, "run_id": state.get("run_id")})


def persist_attempt_completion(task_dir, state, result, output, validation, process_succeeded):
    """Record completion before any canonical artifact can be published."""
    if not result.get("attempt_id"):
        return True  # Compatibility for older tests/adapters and legacy records.
    try:
        attempts_module.record_completion(task_dir, result, output, validation, process_succeeded)
        state.setdefault("durable_attempts", {}).setdefault(result["attempt_id"], {})["status"] = "completed"
        write_state_atomic(task_dir, state)
        return True
    except DurableStorageError:
        return False


def invocation_runtime_paths(task_dir, candidate_path, ledger_path):
    candidate_path = Path(candidate_path)
    name = candidate_path.name
    base = name[:-len(".candidate.md")] if name.endswith(".candidate.md") else name
    runs = candidate_path.parent
    allowed = [
        candidate_path,
        runs / (base + ".stdout"),
        runs / (base + ".stderr"),
        runs / (base + ".json"),
        runs / (base + ".reasoning.md"),
        orchestrator_dir(task_dir) / "lock.json",
    ]
    if ledger_path is not None:
        allowed.append(ledger_path)
    return allowed


def capture_protected_integrity(task_dir, excluded_paths):
    config_path = config.CONFIG_PATH if config.CONFIG_PATH.is_absolute() else REPO_ROOT / config.CONFIG_PATH
    return integrity_module.capture_paths(
        [task_dir, config_path.parent, USAGE_ROOT],
        excluded_paths=excluded_paths,
    )


def invocation_postcondition(task_dir, result, execution_mode):
    """Apply the one C07 postcondition used by every agent attempt."""
    source_after = None
    source_after_error = None
    if execution_mode == "read-only":
        source_after, source_after_error = current_source_identity(task_dir)
    excluded = result.pop("_protected_excluded_paths", [])
    protected_before = result.pop("_protected_before", None)
    protected_before_error = result.pop("_protected_before_error", None)
    try:
        protected_after = capture_protected_integrity(task_dir, excluded)
        protected_after_error = None
    except integrity_module.IntegrityError as exc:
        protected_after = None
        protected_after_error = str(exc)

    problems = []
    ownership_error = None
    ownership_expected = result.pop("_ownership_expected", None)
    if ownership_expected is not None:
        try:
            validate_execution_ownership(
                task_dir,
                REPO_ROOT,
                ownership_expected.get("run_id"),
                controller_pid=ownership_expected.get("controller_pid"),
            )
        except (LockError, TypeError, ValueError) as exc:
            ownership_error = str(exc)
            problems.append("execution ownership postcondition failed: " + ownership_error)
    source_changed = []
    if execution_mode == "read-only":
        source_before = result.get("_source_before")
        source_before_error = result.get("_source_before_error")
        if source_before_error or source_after_error:
            problems.append("source identity comparison unavailable: " + (source_before_error or source_after_error))
        elif not same_identity(source_before, source_after):
            source_changed = changed_identity_paths(source_before, source_after)
            problems.append("read-only agent changed source content")

    protected_changed = []
    if protected_before_error or protected_after_error or protected_before is None or protected_after is None:
        problems.append("protected-record comparison unavailable: " + (protected_before_error or protected_after_error or "missing pre-dispatch snapshot"))
    else:
        protected_changed = integrity_module.changed_paths(protected_before, protected_after)
        if protected_changed:
            problems.append("agent changed protected task, policy, or controller records")

    postcondition = {
        "valid": not problems,
        "reason": "; ".join(problems) if problems else "source and protected records satisfy the invocation postcondition",
        "source_after": source_after,
        "source_after_error": source_after_error,
        "changed_paths": source_changed,
        "protected_changed_paths": protected_changed,
        "ownership_error": ownership_error,
    }
    result["postcondition"] = postcondition
    return postcondition


def block_postcondition_failure(task_dir, state, stage_key, result, agent, postcondition):
    preserve_failed(
        task_dir,
        stage_key,
        read_candidate(result),
        "invocation_postcondition_failed",
        {
            "agent": agent,
            "metadata_path": result.get("metadata_path"),
            "reason": postcondition["reason"],
            "changed_paths": postcondition["changed_paths"],
            "protected_changed_paths": postcondition["protected_changed_paths"],
        },
    )
    if stage_key in ("overseer", "07"):
        existing = [key for key in ("06", "07", "08") if (task_dir / CONTRACTS[key].filename).exists()]
        if existing:
            invalidate_evidence(task_dir, state, existing, postcondition["reason"], keep_in_place=(CONTRACTS["06"].filename,))
    append_log(task_dir, {
        "event": "invocation_postcondition_failed",
        "stage": stage_key,
        "reason": postcondition["reason"],
        "changed_paths": postcondition["changed_paths"],
        "protected_changed_paths": postcondition["protected_changed_paths"],
        "run_id": state.get("run_id"),
    })
    block_transition(task_dir, state, stage_key, postcondition["reason"], FAILURE_CLASS_SANDBOX_ENVIRONMENT)


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
        if execution_mode == "read-only" and detail.get("read_only", True) is not True:
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
    return gates_module.accepted_stage4_gate(task_dir)


def record_gate_pass(task_dir, state, pass_number, gate):
    return gates_module.record_gate_pass(task_dir, state, pass_number, gate)


def archive_stage4_brief_pass(task_dir, pass_number):
    return gates_module.archive_stage4_brief_pass(task_dir, pass_number)


def gate_classification(gate):
    return gates_module.gate_classification(gate)


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
    return stage5_module.stage5_report_provenance(task_dir, state)


def stage5_run_matches_report(run, report_path, report_hash, state):
    return stage5_module.stage5_run_matches_report(run, report_path, report_hash, state)


def stage5_postprocessing_complete(task_dir, state):
    return stage5_module.stage5_postprocessing_complete(task_dir, state, report_func=stage5_report_provenance)


def any_stage5_postprocessing_present(task_dir, state):
    return stage5_module.any_stage5_postprocessing_present(task_dir, state)


def checkpoint_noop_eligible(task_dir, state):
    return stage5_module.checkpoint_noop_eligible(task_dir, state, postprocessing_func=stage5_postprocessing_complete)


def checkpoint_hashes(task_dir, state):
    return stage5_module.checkpoint_hashes(task_dir, state)


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
            postcondition = invocation_postcondition(task_dir, result, "read-only")
            if not postcondition["valid"]:
                persist_attempt_completion(task_dir, state, result, read_candidate(result), {"valid": False}, False)
                block_postcondition_failure(task_dir, state, "overseer", result, agent, postcondition)
                return None
            text = read_candidate(result)
            overseer_process_succeeded = successful_process_result(result)
            overseer_validation = {"valid": overseer_process_succeeded and bool(text.strip())}
            if not persist_attempt_completion(task_dir, state, result, text, overseer_validation, overseer_process_succeeded):
                block_transition(task_dir, state, "overseer", "durable overseer completion persistence failed", FAILURE_CLASS_SOURCE_FAILURE)
                return None
            if not successful_process_result(result):
                preserve_failed(task_dir, "overseer", text, result.get("failure_class") or FAILURE_CLASS_UNKNOWN_FAILURE, {"agent": agent, "metadata_path": result.get("metadata_path")})
                raise ValueError("overseer process did not complete successfully")
            parsed = parse_overseer_candidate(text)
            handoff = parsed
            source = agent
            state["overseer"] = {"agent": agent, "status": "generated", "result": result}
            break
        except ManagedProcessInterrupted:
            raise
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

    eligibility = stage6_automatic_eligibility(task_dir, state, config, verification_report, handoff)
    if eligibility["eligible"]:
        handoff = upgrade_to_auto_verified(handoff, verification_report)

    paths = write_handoff_files(task_dir, handoff, source)
    state["overseer"] = dict(state.get("overseer") or {}, **paths)
    return handoff


def automatic_verification_eligibility(config, verification_report, handoff):
    """Return the controller's Stage 6 automatic-completion decision."""
    route = (handoff or {}).get("route") if isinstance(handoff, dict) else None
    if route in ("blocked", "administrator_action"):
        return {"eligible": False, "reason": "automatic Stage 6 completion is prohibited by the %s handoff" % route}
    if not config.get("enable_auto_verified", True):
        return {"eligible": False, "reason": "automatic Stage 6 completion is disabled"}
    if not isinstance(verification_report, dict):
        return {"eligible": False, "reason": "automatic verification evidence is missing or malformed"}
    if verification_report.get("overall_status") != "passed":
        return {"eligible": False, "reason": "automatic verification did not produce a passing report"}

    configured = verification_report.get("driven_project_checks_configured")
    count = verification_report.get("driven_project_check_count")
    checks = verification_report.get("checks")
    if configured is not True or isinstance(count, bool) or not isinstance(count, int) or count < 1 or not isinstance(checks, list):
        return {"eligible": False, "reason": "automatic verification requires at least one configured driven-project check"}
    driven_checks = [
        check for check in checks
        if isinstance(check, dict) and str(check.get("name", "")).startswith("driven_project_")
    ]
    if len(driven_checks) != count or verification_report.get("driven_project_verified") is not True:
        return {"eligible": False, "reason": "configured driven-project verification evidence is missing or inconsistent"}
    if any(check.get("status") != "passed" for check in driven_checks):
        return {"eligible": False, "reason": "not all configured driven-project checks passed"}

    coverage = verification_report.get("test_coverage_delta_signal")
    if not isinstance(coverage, dict) or coverage.get("status") not in ("ok", "no_data"):
        if isinstance(coverage, dict) and coverage.get("status") == "flagged":
            return {"eligible": False, "reason": "verification reported a flagged coverage signal"}
        return {"eligible": False, "reason": "coverage evidence is missing or malformed"}
    return {"eligible": True, "reason": "controller verification evidence is eligible for automatic Stage 6 completion"}


def render_auto_stage06_notes(verification_report, identity=None):
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
        "## Source identity",
        "",
        format_identity(identity) if identity else "unavailable",
        "",
        "## Decision",
        "",
        "- [x] Accept",
        "- [ ] Reject",
        "- [ ] Needs follow-up",
    ])
    return "\n".join(lines).rstrip() + "\n"


last_nonempty_line = decision_module.last_nonempty_line
render_stage08_decision = decision_module.render_stage08_decision
worse_decision = decision_module.worse_decision


def ensure_stage08_decision(task_dir, state):
    return decision_module.ensure_stage08_decision(task_dir, state, block_transition)


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


def successful_process_result(result):
    """The shared promotion boundary for one-shot agent adapters."""
    return (
        isinstance(result, dict)
        and result.get("exit_code") == 0
        and result.get("failure_class") is None
        and result.get("status") in (None, "passed")
    )


def current_source_baseline(task_dir=None):
    try:
        return capture_writer_source_baseline(task_dir)
    except Exception:
        return None


def capture_writer_source_baseline(task_dir=None):
    baseline = capture_dirty_baseline(REPO_ROOT)
    identity = capture_source_identity(REPO_ROOT, controller_owned_paths(task_dir))
    baseline["head"] = identity.get("head")
    baseline["source_identity"] = identity
    return baseline


def same_source_baseline(left, right):
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    if left.get("source_identity") is not None or right.get("source_identity") is not None:
        return same_identity(left.get("source_identity"), right.get("source_identity"))
    return (
        left.get("head") == right.get("head")
        and left.get("entries") == right.get("entries")
        and left.get("hashes") == right.get("hashes")
        and left.get("modes") == right.get("modes")
    )


def writer_source_changes(task_dir, before):
    """Compare a writer baseline with current source, returning (changes, error)."""
    if not isinstance(before, dict):
        return None, "pre-dispatch source baseline is missing"
    try:
        after = capture_writer_source_baseline(task_dir)
        changes = changed_files_since(REPO_ROOT, before)
        before_identity = before.get("source_identity")
        after_identity = after.get("source_identity")
        if before_identity is None:
            if changes or before.get("head") != after.get("head"):
                if before.get("head") != after.get("head"):
                    changes.append({"path": "<git-head>", "reason": "head_changed_during_failed_writer"})
                return changes, None
            return None, "pre-dispatch source baseline lacks complete source identity"
        if not same_identity(before_identity, after_identity):
            identity_paths = changed_identity_paths(before_identity, after_identity)
            allowed_paths = set()
            for path in identity_paths:
                if path in ("<HEAD>", "<index>"):
                    allowed_paths.add(path)
                elif ":" in path:
                    allowed_paths.add(path.split(":", 1)[1])
                else:
                    allowed_paths.add(path)
            # The legacy porcelain baseline does not know all controller-owned
            # paths. Keep its useful reason labels only for paths independently
            # confirmed by the complete source identity.
            changes = [item for item in changes if item.get("path") in allowed_paths]
            known = {(item.get("path"), item.get("reason")) for item in changes}
            for path in identity_paths:
                if path == "<HEAD>":
                    item = {"path": "<git-head>", "reason": "head_changed_during_failed_writer"}
                elif path == "<index>":
                    item = {"path": "<git-index>", "reason": "index_changed_during_failed_writer"}
                elif path.startswith("index:"):
                    item = {"path": path[6:], "reason": "index_changed_during_failed_writer"}
                elif ":" in path:
                    item = {"path": path.split(":", 1)[1], "reason": "source_identity_changed_during_failed_writer"}
                else:
                    item = {"path": path, "reason": "source_identity_changed_during_failed_writer"}
                marker = (item["path"], item["reason"])
                if marker not in known:
                    changes.append(item)
                    known.add(marker)
        else:
            changes = []
        return changes, None
    except Exception as exc:
        return None, str(exc)


def failed_writer_requires_approval(task_dir, state, stage_key, execution_mode, agent, result, failure_class):
    """Block a second writer when a failed writer changed source, or when
    the pre/post source comparison cannot be trusted."""
    if execution_mode != "workspace-write":
        return False
    before = result.get("_source_before")
    comparison_error = result.get("_source_before_error")
    changes = None
    if comparison_error is None:
        changes, comparison_error = writer_source_changes(task_dir, before)
    if not changes and comparison_error is None:
        if result.get("attempt_id"):
            attempts_module.record_writer_resolution(
                task_dir, result.get("attempt_id"), stage_key, "source_unchanged"
            )
        return False
    result["failed_writer_source_changes"] = changes
    if comparison_error is not None:
        result["failed_writer_source_comparison_error"] = comparison_error
    detail = "source changed after failed write-capable attempt" if changes else "source comparison is unavailable after failed write-capable attempt"
    require_retry_approval(
        state,
        stage_key,
        failure_class or FAILURE_CLASS_UNKNOWN_FAILURE,
        agent,
        detail,
        retry_type="failed_write_source_change",
        failed_attempt_metadata_path=result.get("metadata_path"),
        failed_attempt_number=result.get("attempt_number"),
        failed_attempt_id=result.get("attempt_id"),
        failed_source_changes=changes,
        failed_source_comparison_error=comparison_error,
    )
    write_state_atomic(task_dir, state)
    append_log(task_dir, {"event": "failed_writer_retry_approval_required", "stage": stage_key, "attempt": result.get("attempt_number"), "source_changes": changes, "comparison_error": comparison_error, "run_id": state.get("run_id")})
    return True


def recover_failed_writer_approval(task_dir, state, dispatch, completed):
    """Recreate approval lost after a failed writer's durable completion."""
    pending = state.get("pending_approval") or {}
    attempt_id = dispatch.get("attempt_id")
    if (pending.get("failed_attempt_id") == attempt_id or
            (pending.get("stage") == dispatch.get("stage") and
             pending.get("failed_attempt_number") == dispatch.get("attempt_number"))):
        if pending.get("approved") and not pending.get("consumed"):
            return None
        return "failed writer attempt %s is awaiting retry approval" % attempt_id
    changes, comparison_error = writer_source_changes(task_dir, dispatch.get("implementation_baseline"))
    if not changes and comparison_error is None:
        attempts_module.record_writer_resolution(
            task_dir, attempt_id, dispatch.get("stage"), "source_unchanged_on_recovery"
        )
        return None
    result = completed.get("result") or {}
    detail = "source changed after failed write-capable attempt" if changes else "source comparison is unavailable after failed write-capable attempt"
    require_retry_approval(
        state,
        dispatch.get("stage"),
        result.get("failure_class") or FAILURE_CLASS_UNKNOWN_FAILURE,
        dispatch.get("agent"),
        detail,
        retry_type="failed_write_source_change",
        failed_attempt_metadata_path=result.get("metadata_path"),
        failed_attempt_number=dispatch.get("attempt_number"),
        failed_attempt_id=attempt_id,
        failed_source_changes=changes,
        failed_source_comparison_error=comparison_error,
    )
    write_state_atomic(task_dir, state)
    return "failed writer attempt %s requires source-bound retry approval" % attempt_id


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
            acknowledge_consumed_inputs(task_dir, state, stage_key)
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
            acknowledge_consumed_inputs(task_dir, state, stage_key)
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
        preserve_failed(task_dir, stage_key, response["output"], FAILURE_CLASS_MAX_TURNS, {"agent": agent})
        validation = validate_text(response["output"], CONTRACTS[stage_key])
        if (validation["valid"] or useful_partial(response["output"], CONTRACTS[stage_key])) and not state["attempts"].get(stage_key + "_completion_retry"):
            state["attempts"][stage_key + "_completion_retry"] = 1
            increment_agent_count(state, agent)
            completion = mock.invoke(agent, stage_key, attempt + 1, completion_only=True)
            if completion.get("failure_class") is None:
                final = atomic_finalize(task_dir, stage_key, completion["output"])
            else:
                final = {"finalized": False, "validation": validate_text(completion.get("output", ""), CONTRACTS[stage_key])}
            if final["finalized"]:
                acknowledge_consumed_inputs(task_dir, state, stage_key)
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
    failed_attempt_id=None,
    completion_retry_metadata_path=None,
    completion_retry_attempt_number=None,
    failed_source_changes=None,
    failed_source_comparison_error=None,
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
        ("failed_attempt_id", failed_attempt_id),
        ("completion_retry_metadata_path", completion_retry_metadata_path),
        ("completion_retry_attempt_number", completion_retry_attempt_number),
        ("failed_source_changes", failed_source_changes),
        ("failed_source_comparison_error", failed_source_comparison_error),
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
    run_root = Path(tempfile.mkdtemp(prefix="catenna-mock-runs-"))
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
        shutil.rmtree(str(run_root), ignore_errors=True)
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
