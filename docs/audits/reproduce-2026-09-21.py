"""Isolated audit reproductions; no real coding-agent or network calls."""
import contextlib
import copy
import io
import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from agent_pipeline import artifacts, config, controller, real_runner, verification
from agent_pipeline.locking import TaskLock
from agent_pipeline.mock_agent import valid_artifact
from agent_pipeline.state import CONTRACTS, load_state, new_state, state_path
from agent_pipeline.tests.test_real_pipeline import RealPipelineTests


def emit(name, **detail):
    print(json.dumps(dict(probe=name, **detail), sort_keys=True))


@contextlib.contextmanager
def fixture():
    test = RealPipelineTests()
    test.setUp()
    try:
        yield test
    finally:
        test.tearDown()


def run_pipeline(test):
    with contextlib.redirect_stdout(io.StringIO()):
        return controller.pipeline_run(test.task, allow_dirty=True)


with fixture() as test:
    cfg = test.config()
    cfg["enable_auto_verified"] = False
    failed = test.verification_report("failed", "flagged", False)
    original_invoke = controller.invoke_stage

    def forged_overseer(*args, **kwargs):
        result = original_invoke(*args, **kwargs)
        if args[3] == "overseer":
            candidate = Path(result["candidate_artifact_path"])
            handoff = json.loads(candidate.read_text())
            handoff["route"] = "auto_verified"
            candidate.write_text(json.dumps(handoff))
        return result

    with patch.object(controller, "load_config", return_value=cfg), patch.object(controller, "invoke_stage", side_effect=forged_overseer), patch.object(verification, "run_verification", return_value=failed):
        code = run_pipeline(test)
    final = artifacts.manual_test_decision((test.task_dir / CONTRACTS["08"].filename).read_text())
    emit("agent_bypasses_failed_verification_and_disabled_auto_verify", exit_code=code, decision=final)
    assert code == 0 and final == "accept"


for phrase in ("Not approved.", "Tests did not pass.", "Do not accept this change."):
    notes = "# Stage 6 - Manual test notes\n\n## Decision\n\n" + phrase + "\n"
    result = artifacts.validate_text(notes, CONTRACTS["06"])
    decision = artifacts.manual_test_decision(notes)
    emit("negative_manual_outcome", phrase=phrase, valid=result["valid"], decision=decision)
    assert result["valid"] and decision == "accept"


with fixture() as test:
    passed = test.verification_report("passed", "ok", True)
    with patch.object(verification, "run_verification", return_value=passed) as verify:
        first = run_pipeline(test)
        (test.root / "broken.py").write_text("raise RuntimeError('untested change')\n")
        second = run_pipeline(test)
    emit("accepted_task_after_source_change", initial_exit=first, subsequent_exit=second, verification_calls=verify.call_count)
    assert first == second == 0 and verify.call_count == 1


with fixture() as test:
    result = test.stage_result("07", valid_artifact("07"), failure_class="timeout")
    result.update(agent="claude", provider="claude")
    code, calls = test.run_stage_with_results(new_state(test.task, "probe"), [result], stage_key="07")
    emit("failed_reviewer_artifact_promoted", provider_failure="timeout", exit_code=code, finalized=result.get("finalized"))
    assert code == 0 and result.get("finalized")


with fixture() as test:
    current_snapshot = [""]
    partial = test.stage_result("02", CONTRACTS["02"].heading + "\n\n## Summary\nPartial.\n", "max_turns")
    completed = test.stage_result("02", valid_artifact("02"), attempt_number=2)
    pending = [partial, completed]

    def completion_mutation(*args, **kwargs):
        if kwargs.get("completion_for"):
            current_snapshot[0] = " M tracked.py"
        return pending.pop(0)

    with patch.object(controller, "source_snapshot", side_effect=lambda: current_snapshot[0]), patch.object(controller, "invoke_stage", side_effect=completion_mutation):
        code = controller.ensure_real_stage(test.task_dir, new_state(test.task, "probe"), test.config(), "02", "read-only", {})
    emit("completion_retry_skips_mutation_guard", exit_code=code, source_after=current_snapshot[0])
    assert code == 0 and current_snapshot[0]


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    source = root / "code.py"
    source.write_text("original\n")
    subprocess.run(["git", "add", "code.py"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=Audit", "-c", "user.email=audit@example.invalid", "commit", "-qm", "baseline"], cwd=root, check=True)
    source.write_text("already dirty\n")
    with patch.object(controller, "REPO_ROOT", root):
        before = controller.source_snapshot()
        source.write_text("changed again\n")
        after = controller.source_snapshot()
    emit("dirty_file_mutation_invisible", before=before, after=after, snapshots_equal=before == after)
    assert before == after
    task_a = root / ".agent-pipeline" / "tasks" / "a"
    task_b = root / ".agent-pipeline" / "tasks" / "b"
    with TaskLock(task_a, "run", "a"), TaskLock(task_b, "run", "b"):
        emit("different_tasks_share_workspace_without_exclusion", both_locks_acquired=True)


with fixture() as test:
    original_reconcile = controller.reconcile_artifacts

    def crash_after_stage5(task_dir, state, **kwargs):
        if state.get("real_stage_runs", {}).get("05"):
            raise RuntimeError("simulated controller crash after artifact promotion")
        return original_reconcile(task_dir, state, **kwargs)

    try:
        with patch.object(controller, "reconcile_artifacts", side_effect=crash_after_stage5):
            run_pipeline(test)
    except RuntimeError:
        pass
    state_saved_at_crash = state_path(test.task_dir).exists()
    code = run_pipeline(test)
    state = load_state(test.task_dir, test.task)
    emit("stage5_crash_loses_provenance", state_saved_at_crash=state_saved_at_crash, resumed_exit=code, failure=state.get("last_failure"))
    assert not state_saved_at_crash and code != 0 and "provenance" in state["last_failure"]["reason"]


with fixture() as test:
    cfg = copy.deepcopy(config.DEFAULT_CONFIG)
    candidate = test.task_dir / "candidate.md"
    prompt = test.task_dir / "prompt.txt"
    prompt.write_text("probe")
    low = dict(cfg["agents"]["codex"], read_effort="low")
    high = dict(cfg["agents"]["codex"], read_effort="high")
    argv_low, _ = real_runner.build_argv("codex", low, "read-only", prompt, candidate, cfg, "02")
    argv_high, _ = real_runner.build_argv("codex", high, "read-only", prompt, candidate, cfg, "02")
    emit("codex_effort_ignored", low_equals_high=argv_low == argv_high, argv=argv_low)
    assert argv_low == argv_high


with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "config.json"
    path.write_text("[]")
    try:
        config.load_config(path)
    except Exception as exc:
        emit("non_object_config", exception=type(exc).__name__, message=str(exc))
        assert isinstance(exc, AttributeError)

emit("done", outcome="all audit reproductions confirmed")
