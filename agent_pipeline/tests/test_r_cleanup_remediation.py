"""R08-FR1 regressions: the 2026-09-25 cross-slice review probes, inverted.

Each defect D1-D8 in docs/v3prep/v2-cleanup-gate.md was reproduced by a
temporary probe script. These tests replay the same scenarios against the real
controller, a real temporary Git worktree, fake agent CLIs, and (for
interruption) real local subprocesses, and assert the corrected behavior.
"""

from __future__ import print_function

import io
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from agent_pipeline import attempts, controller, source_identity
from agent_pipeline.failures import (
    EXIT_BLOCKED,
    EXIT_INTERRUPTED,
    EXIT_SUCCESS,
    FAILURE_CLASS_PROCESS_INTERRUPTED,
    FAILURE_CLASS_UNKNOWN_FAILURE,
)
from agent_pipeline.locking import ExecutionOwnership, LockError, lock_path, worktree_lock_path
from agent_pipeline.mock_agent import valid_artifact
from agent_pipeline.overseer import parse_overseer_candidate
from agent_pipeline.runner import atomic_finalize
from agent_pipeline.state import CONTRACTS, STAGE_ORDER, load_state, new_state, reconcile_artifacts
from agent_pipeline.tests import test_c06_evidence as c06
from agent_pipeline.tests import test_overseer as overseer_tests
from agent_pipeline.tests import test_real_pipeline as base


class RemediationProbeFixture(unittest.TestCase):
    # Borrow the C06 real-worktree fixture without re-collecting its tests.
    setUp = base.RealPipelineTests.setUp
    tearDown = base.RealPipelineTests.tearDown
    config = base.RealPipelineTests.config
    write_fake_agent = base.RealPipelineTests.write_fake_agent
    verification_report = base.RealPipelineTests.verification_report
    manual_notes = base.RealPipelineTests.manual_notes
    start = c06.C06EvidenceTests.start
    git = c06.C06EvidenceTests.git
    dispatches = c06.C06EvidenceTests.dispatches
    state = c06.C06EvidenceTests.state
    identity = c06.C06EvidenceTests.identity
    status_output = c06.C06EvidenceTests.status_output
    pipeline = c06.C06EvidenceTests.pipeline
    accept = c06.C06EvidenceTests.accept
    write_notes = c06.C06EvidenceTests.write_notes

    def use_config(self, cfg):
        original = controller.load_config
        controller.load_config = lambda: cfg
        self.addCleanup(setattr, controller, "load_config", original)
        return cfg

    def reason(self):
        return (self.state().get("last_failure") or {}).get("reason") or ""


class D1ConsumedInputRecoveryTests(RemediationProbeFixture):
    """D1 (R01): an edited request after acceptance is never re-acknowledged."""

    def test_d1_request_edit_after_acceptance_revokes_and_redispatches(self):
        self.start()
        self.accept()
        stage02_before = self.dispatches("02")
        request = self.task_dir / CONTRACTS["00"].filename
        request.write_text(
            request.read_text(encoding="utf-8")
            + "\nNew mandatory requirement: do not accept the previous implementation.\n",
            encoding="utf-8",
        )

        self.assertIn("current_acceptance: no", self.status_output())
        self.assertEqual(self.pipeline(), EXIT_SUCCESS)
        self.assertEqual(self.dispatches("02"), stage02_before + 1)
        self.assertIn("current_acceptance: yes", self.status_output())

    def test_d1_recovery_does_not_rewrite_acknowledged_request_hash(self):
        self.start()
        state = self.accept()
        request = self.task_dir / CONTRACTS["00"].filename
        request.write_text(request.read_text(encoding="utf-8") + "\nNew requirement.\n", encoding="utf-8")
        acknowledged = state["input_hashes"].get(CONTRACTS["00"].filename)

        stale_before = reconcile_artifacts(self.task_dir, json.loads(json.dumps(state)), read_only=True)
        attempts.recover(self.task_dir, state, atomic_finalize, stages=STAGE_ORDER)
        stale_after = reconcile_artifacts(self.task_dir, state, read_only=True)

        self.assertIn("02", stale_before)
        self.assertEqual(stale_after, stale_before)
        self.assertEqual(state["input_hashes"].get(CONTRACTS["00"].filename), acknowledged)


class D2ReviewInvalidationAndBudgetTests(RemediationProbeFixture):
    """D2 (R01, R02): invalidated reviews stay historical; budget per identity."""

    def test_d2_recovery_does_not_resurrect_invalidated_review(self):
        self.start()
        state = self.accept()
        controller.invalidate_evidence(self.task_dir, state, ["07", "08"], "R08 probe invalidation")
        self.assertFalse((self.task_dir / CONTRACTS["07"].filename).exists())

        error = attempts.recover(self.task_dir, state, atomic_finalize, stages=("07",))

        self.assertIsNone(error)
        self.assertFalse((self.task_dir / CONTRACTS["07"].filename).exists())

    def test_d2_two_review_config_changes_each_produce_one_review_without_conflict(self):
        self.start()
        cfg = self.use_config(self.config())
        self.accept()
        reviews = self.dispatches("07")

        cfg["turn_budgets"]["07"] += 1
        with mock.patch.dict(os.environ, {"FAKE_STAGE7_VERDICT": "needs_followup"}):
            self.pipeline()
        self.assertEqual(self.dispatches("07"), reviews + 1)
        self.assertNotIn("conflicting successful durable attempts", self.reason())

        cfg["turn_budgets"]["07"] += 1
        self.assertEqual(self.pipeline(), EXIT_SUCCESS)
        self.assertEqual(self.dispatches("07"), reviews + 2)
        self.assertNotIn("conflicting successful durable attempts", self.reason())
        self.assertIn("current_acceptance: yes", self.status_output())

    def test_d2_failing_rereview_is_dispatched_once_despite_repeated_resume(self):
        self.start()
        cfg = self.use_config(self.config())
        self.assertEqual(cfg["stage_attempt_budget"], 1)
        self.accept()
        reviews = self.dispatches("07")
        cfg["turn_budgets"]["07"] += 1

        with mock.patch.dict(os.environ, {"FAKE_FAIL_STAGE": "07"}):
            for _ in range(3):
                self.assertEqual(self.pipeline(), EXIT_BLOCKED)
                self.assertEqual(self.dispatches("07"), reviews + 1)

        reason = self.reason()
        self.assertIn("budget exhausted for review-input identity", reason)
        self.assertIn("approve-retry", reason)


class D3D7FailedWriterApprovalTests(RemediationProbeFixture):
    """D3 and D7 (R03): failed-writer approval is durable and mode-aware."""

    def test_d3_crash_before_approval_bookkeeping_blocks_second_writer(self):
        self.start()
        cfg = self.use_config(self.config())
        cfg["stage_attempt_budget"] = 2
        original = controller.invoke_agent
        writes = []

        def failing_writer(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[3] == "05":
                writes.append(True)
                if len(writes) == 1:
                    (self.root / "partially_changed.py").write_text("partial = True\n", encoding="utf-8")
                    result.update(exit_code=9, status="failed", failure_class=FAILURE_CLASS_UNKNOWN_FAILURE)
            return result

        with mock.patch.object(controller, "invoke_agent", side_effect=failing_writer):
            with mock.patch.object(
                controller, "failed_writer_requires_approval",
                side_effect=SystemExit("crash before approval bookkeeping"),
            ):
                with self.assertRaises(SystemExit):
                    self.pipeline()
            self.assertEqual(self.pipeline(), EXIT_BLOCKED)

        self.assertEqual(len(writes), 1)
        state = self.state()
        self.assertEqual(state["state"], "awaiting_retry_approval")
        self.assertIsNotNone(state["pending_approval"])
        self.assertIn("current_acceptance: no", self.status_output())

    def test_d7_mode_only_change_to_dirty_file_requires_durable_approval(self):
        self.start()
        path = self.root / "script.py"
        path.write_text("value = 1\n", encoding="utf-8")
        self.git("add", "script.py")
        self.git("commit", "-qm", "baseline")
        path.write_text("value = 2\n", encoding="utf-8")
        before = controller.capture_writer_source_baseline()
        identity_before = self.identity()
        path.chmod(0o755)
        state = new_state(self.task, "r08-mode-probe")

        required = controller.failed_writer_requires_approval(
            self.task_dir, state, "05", "workspace-write", "codex",
            {"_source_before": before, "attempt_number": 1}, FAILURE_CLASS_UNKNOWN_FAILURE,
        )

        self.assertFalse(source_identity.same_identity(identity_before, self.identity()))
        self.assertTrue(required)
        self.assertIsNotNone(load_state(self.task_dir, self.task)["pending_approval"])


class D4InterruptionTests(RemediationProbeFixture):
    """D4 (R04): Ctrl-C stops remaining checks and every later dispatch."""

    def test_d4_sigint_in_first_real_check_stops_second_check(self):
        second_marker = self.root / "second.started"
        result_path = self.root / "interrupted.json"
        runs = self.root / "r08-runs"
        runs.mkdir()
        commands = [
            {"name": "first", "argv": [
                sys.executable, "-c",
                "import os, signal, time; time.sleep(0.1); os.kill(os.getppid(), signal.SIGINT); time.sleep(10)",
            ]},
            {"name": "second", "argv": [
                sys.executable, "-c", "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('ran')",
                str(second_marker),
            ]},
        ]
        worker_code = (
            "import json, sys\n"
            "from pathlib import Path\n"
            "from agent_pipeline.verification import run_driven_project_checks\n"
            "from agent_pipeline.real_runner import ManagedProcessInterrupted\n"
            "try:\n"
            "    run_driven_project_checks(Path(%r), Path(%r), %r)\n"
            "except ManagedProcessInterrupted as exc:\n"
            "    checks = [{k: c.get(k) for k in ('name', 'status')} for c in exc.verification_checks]\n"
            "    Path(%r).write_text(json.dumps(checks), encoding='utf-8')\n"
            "    sys.exit(130)\n"
            "sys.exit(0)\n"
        ) % (str(self.root), str(runs), commands, str(result_path))
        repo_root = str(Path(__file__).resolve().parents[2])
        env = dict(os.environ, PYTHONPATH=repo_root + os.pathsep + os.environ.get("PYTHONPATH", ""))
        worker = subprocess.Popen([sys.executable, "-c", worker_code], env=env)
        try:
            worker.wait(timeout=30)
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.wait()

        self.assertEqual(worker.returncode, EXIT_INTERRUPTED)
        checks = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(
            [(check["name"], check["status"]) for check in checks],
            [("driven_project_first", "interrupted"), ("driven_project_second", "not_attempted")],
        )
        time.sleep(0.2)
        self.assertFalse(second_marker.exists(), "second check launched after interruption")

    def test_d4_interrupted_verification_invokes_no_overseer_and_exits_130(self):
        self.start()
        self.verification_outcome = "interrupt"
        stage_calls = []
        original = controller.invoke_stage

        def track(*args, **kwargs):
            stage_calls.append(args[3])
            return original(*args, **kwargs)

        with mock.patch.object(controller, "invoke_stage", side_effect=track):
            self.assertEqual(self.pipeline(), EXIT_INTERRUPTED)

        self.assertEqual(len(self.verification_calls), 1)
        self.assertNotIn("overseer", stage_calls)
        self.assertNotIn("07", stage_calls)
        state = self.state()
        self.assertNotEqual(state["state"], "complete")
        self.assertEqual(state["last_failure"]["failure_class"], FAILURE_CLASS_PROCESS_INTERRUPTED)
        self.assertEqual(state["evidence"]["verification"][-1]["status"], "interrupted")


class D5ManualAcceptanceTests(RemediationProbeFixture):
    """D5 (R05): a newer failed configured check revokes manual acceptance."""

    def verify(self):
        with redirect_stdout(io.StringIO()):
            return controller.pipeline_verify(self.task)

    def assert_manual_accept_blocked(self, reviews):
        summary = controller.current_evidence_summary(self.task_dir, self.state())
        self.assertFalse(summary["verification"]["current"])
        self.assertFalse(summary["current_acceptance"])
        self.assertIn("current_acceptance: no", self.status_output())
        self.assertEqual(self.pipeline(), EXIT_BLOCKED)
        self.assertEqual(self.dispatches("07"), reviews)
        reason = self.state()["human_checkpoint"]["reason"]
        self.assertIn("`required`", reason)
        self.assertIn("fix the source and re-run `catenna verify`", reason)

    def test_d5_manual_accept_does_not_survive_newer_failed_check(self):
        self.start()
        cfg = self.use_config(self.config())
        cfg["enable_auto_verified"] = False
        cfg["verification"]["driven_project_commands"] = [{"name": "required", "argv": ["false"]}]

        # Probe order: failed check, then manual Accept notes, then verify.
        self.verification_outcome = "failed"
        self.assertEqual(self.pipeline(), EXIT_BLOCKED)
        self.write_notes(self.manual_notes())
        reviews = self.dispatches("07")
        self.assertNotEqual(self.verify(), EXIT_SUCCESS)
        self.assert_manual_accept_blocked(reviews)

        # The defect proper: a current manual Accept, then a newer failure.
        self.verification_outcome = "passed"
        self.assertEqual(self.verify(), EXIT_SUCCESS)
        self.assertEqual(self.pipeline(), EXIT_SUCCESS)
        self.assertIn("current_acceptance: yes", self.status_output())
        reviews = self.dispatches("07")
        self.verification_outcome = "failed"
        self.assertNotEqual(self.verify(), EXIT_SUCCESS)
        self.assert_manual_accept_blocked(reviews)


class D6OwnershipRecordTests(RemediationProbeFixture):
    """D6 (R06): deleting an ownership record fails the postcondition."""

    def postcondition_after_deleting(self, record):
        state = new_state(self.task, "r08-lock-probe-" + record.split()[0])
        candidate = self.task_dir / ".orchestrator" / "runs" / "probe.candidate.md"
        outcome = None
        try:
            with ExecutionOwnership(self.task_dir, self.root, "probe", state["run_id"], self.task):
                excluded = controller.invocation_runtime_paths(self.task_dir, candidate, None)
                # The same pre-dispatch fields dispatch records for every attempt.
                result = {
                    "_source_before": self.identity(),
                    "_protected_before": controller.capture_protected_integrity(self.task_dir, excluded),
                    "_protected_excluded_paths": [str(path) for path in excluded],
                    "_ownership_expected": {"run_id": state["run_id"], "controller_pid": os.getpid()},
                }
                target = worktree_lock_path(self.root) if record == "worktree execution lock" else lock_path(self.task_dir)
                target.unlink()
                outcome = controller.invocation_postcondition(self.task_dir, result, "read-only")
        except LockError:
            # Releasing ownership may also notice the missing record; the
            # postcondition outcome above is the evidence under test.
            pass
        finally:
            for path in (lock_path(self.task_dir), worktree_lock_path(self.root)):
                if path.exists():
                    path.unlink()
        return outcome

    def test_d6_deleted_worktree_or_task_lock_fails_postcondition(self):
        self.start()
        for record in ("worktree execution lock", "task lock"):
            with self.subTest(record=record):
                outcome = self.postcondition_after_deleting(record)
                self.assertIsNotNone(outcome)
                self.assertFalse(outcome["valid"])
                self.assertIn(record, outcome["ownership_error"])
                self.assertIn("execution ownership postcondition failed", outcome["reason"])

    def test_d6_agent_deleting_worktree_lock_blocks_promotion_and_dispatch(self):
        self.start()
        sabotage = self.root / "r08_sabotage_agent.py"
        sabotage.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            "pathlib.Path(%r).unlink()\n"
            "output = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])\n"
            "output.write_text(%r, encoding='utf-8')\n"
            % (str(worktree_lock_path(self.root)), valid_artifact("02")),
            encoding="utf-8",
        )
        sabotage.chmod(0o755)
        cfg = self.config()
        cfg["agents"]["codex"]["command"] = str(sabotage)
        state = new_state(self.task, "r08-agent-lock")
        code = None
        try:
            with ExecutionOwnership(self.task_dir, self.root, "run", state["run_id"], self.task):
                code = controller.ensure_real_stage(self.task_dir, state, cfg, "02", "read-only", {})
        finally:
            for path in (lock_path(self.task_dir), worktree_lock_path(self.root)):
                if path.exists():
                    path.unlink()

        self.assertEqual(code, EXIT_BLOCKED)
        self.assertFalse((self.task_dir / CONTRACTS["02"].filename).exists())
        self.assertIn("worktree execution lock", state["last_failure"]["reason"])
        self.assertEqual(len(state["real_stage_runs"]["02"]), 1)


class D8SnapshotConsistencyTests(RemediationProbeFixture):
    """D8 (R07): a mid-capture edit never yields an accepted mixed identity."""

    def commit_file(self):
        path = self.root / "a.py"
        path.write_text("value = 1\n", encoding="utf-8")
        self.git("add", "a.py")
        self.git("commit", "-qm", "baseline")
        return path

    def capture_with_mutation(self, path, times):
        original = source_identity._entry
        mutations = []

        def mutate_after_hash(top, rel, allow_missing):
            result = original(top, rel, allow_missing)
            if rel == "a.py" and len(mutations) < times:
                mutations.append(True)
                path.write_text("value = %d\n" % (len(mutations) + 1), encoding="utf-8")
            return result

        with mock.patch.object(source_identity, "_entry", side_effect=mutate_after_hash):
            return source_identity.capture_source_identity(self.root)

    def test_d8_persistent_mid_capture_edit_raises_instead_of_mixed_identity(self):
        self.start()
        path = self.commit_file()
        with self.assertRaises(source_identity.SourceIdentityError):
            self.capture_with_mutation(path, times=100)

    def test_d8_single_mid_capture_edit_retries_to_the_final_content(self):
        self.start()
        path = self.commit_file()
        before = source_identity.capture_source_identity(self.root)

        raced = self.capture_with_mutation(path, times=1)
        actual = source_identity.capture_source_identity(self.root)

        self.assertIsNone(source_identity.identity_problem(raced))
        self.assertFalse(source_identity.same_identity(before, raced))
        self.assertTrue(source_identity.same_identity(raced, actual))


class AutomaticAcceptanceGuardTests(RemediationProbeFixture):
    """A1 stays closed after remediation: invalid evidence cannot auto-accept,
    and valid controller evidence still reaches automatic acceptance."""

    def test_a1_agent_auto_verified_claim_is_rejected_as_invalid_evidence(self):
        with self.assertRaises(ValueError):
            parse_overseer_candidate(overseer_tests.valid_payload(route="auto_verified"))

    def test_a1_invalid_verification_evidence_stays_ineligible(self):
        valid = self.verification_report(overall_status="passed", coverage_status="ok", driven_project_verified=True)
        manual = {"route": "manual_test"}
        cases = [
            ("blocked handoff", self.config(), valid, {"route": "blocked"}),
            ("automation disabled", dict(self.config(), enable_auto_verified=False), valid, manual),
            ("missing report", self.config(), None, manual),
            ("interrupted report", self.config(),
             self.verification_report(overall_status="interrupted", coverage_status="ok", driven_project_verified=False),
             manual),
            ("failed report", self.config(),
             self.verification_report(overall_status="failed", coverage_status="ok", driven_project_verified=True),
             manual),
            ("flagged coverage", self.config(),
             self.verification_report(overall_status="passed", coverage_status="flagged", driven_project_verified=True),
             manual),
            ("unverified driven project", self.config(),
             self.verification_report(overall_status="passed", coverage_status="ok"), manual),
        ]
        for label, cfg, report, handoff in cases:
            with self.subTest(case=label):
                eligibility = controller.automatic_verification_eligibility(cfg, report, handoff)
                self.assertFalse(eligibility["eligible"])
                self.assertTrue(eligibility["reason"])
        self.assertTrue(controller.automatic_verification_eligibility(self.config(), valid, manual)["eligible"])

    def test_a1_valid_controller_evidence_reaches_automatic_acceptance(self):
        self.start()
        self.accept()
        state = self.state()
        self.assertEqual(state["state"], "complete")
        self.assertEqual(state["evidence"]["stage06"][-1]["route"], "auto")
        self.assertTrue(state["evidence"]["verification"][-1]["source_current"])
        self.assertIn("- [x] Accept", (self.task_dir / CONTRACTS["08"].filename).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
