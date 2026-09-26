"""F03 regressions: no retry approval is created or consumed when no attempt can follow."""

from __future__ import print_function

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

from agent_pipeline import attempts, controller
from agent_pipeline.failures import EXIT_BAD_INPUT, EXIT_BLOCKED, EXIT_SUCCESS, FAILURE_CLASS_MAX_TURNS
from agent_pipeline.mock_agent import valid_artifact
from agent_pipeline.state import CONTRACTS, load_state, new_state
from agent_pipeline.tests import test_real_pipeline as base
from agent_pipeline.tests.test_r03_failed_writer_approval import R03FailedWriterApprovalTests


TOUCHED = "writer-touched.txt"


class F03Base(unittest.TestCase):
    setUp = base.RealPipelineTests.setUp
    tearDown = base.RealPipelineTests.tearDown
    config = base.RealPipelineTests.config
    verification_report = base.RealPipelineTests.verification_report
    stage_result = base.RealPipelineTests.stage_result
    run_stage_with_results = base.RealPipelineTests.run_stage_with_results
    durable_failed_writer = R03FailedWriterApprovalTests.durable_failed_writer

    def write_fake_agent(self):
        """The shared fake agent, plus a writer that edits real source before
        failing (FAKE_WRITER_TOUCH) and per-stage output suffixes."""
        path = base.RealPipelineTests.write_fake_agent(self)
        script = path.read_text(encoding="utf-8")
        marker = 'if os.environ.get("FAKE_FAIL_STAGE") == locals().get("stage"):'
        self.assertEqual(script.count(marker), 1)
        script = script.replace(
            "if output_path:\n",
            'text += os.environ.get("FAKE_SUFFIX_" + str(locals().get("stage")), "")\n'
            "if output_path:\n",
            1,
        )
        script = script.replace(
            marker,
            'touch = os.environ.get("FAKE_WRITER_TOUCH")\n'
            'if touch and locals().get("stage") == "05":\n'
            '    with open(touch, "a") as handle:\n'
            '        handle.write("partial write\\n")\n' + marker,
            1,
        )
        path.write_text(script, encoding="utf-8")
        return path

    def use_budget(self, budget):
        cfg = dict(self.config(), stage_attempt_budget=budget)
        controller.load_config = lambda: cfg
        return cfg

    def env(self, **values):
        for key, value in values.items():
            os.environ[key] = value
            self.addCleanup(lambda key=key: os.environ.pop(key, None))

    def unset(self, *keys):
        for key in keys:
            os.environ.pop(key, None)

    def fail_stage5_writer(self):
        self.env(
            FAKE_COUNT_PATH=str(self.root / "counts.txt"),
            FAKE_FAIL_STAGE="05",
            FAKE_WRITER_TOUCH=str(self.root / TOUCHED),
        )

    def pipeline(self):
        with redirect_stdout(io.StringIO()):
            code = controller.pipeline_run(self.task, allow_dirty=True)
        return code, load_state(self.task_dir, self.task)

    def approve(self, approval_id):
        out = io.StringIO()
        with redirect_stdout(out):
            code = controller.approve_retry(self.task, approval_id)
        return code, out.getvalue()

    def dispatches(self, stage):
        path = self.root / "counts.txt"
        if not path.exists():
            return 0
        return path.read_text(encoding="utf-8").splitlines().count(stage)

    def dispatch_records(self, stage):
        records = []
        root = attempts.attempts_root(self.task_dir)
        if not root.exists():
            return records
        for directory in root.iterdir():
            path = directory / "dispatch.json"
            if path.is_file():
                record = json.loads(path.read_text(encoding="utf-8"))
                if record.get("stage") == stage:
                    records.append(record)
        return sorted(records, key=lambda record: record["attempt_number"])

    def log_events(self):
        path = controller.orchestrator_dir(self.task_dir) / "log.jsonl"
        return [json.loads(line)["event"] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def assert_exhaustion_reason(self, reason, stage, path=None):
        self.assertIn("Stage %s" % stage, reason)
        if path is not None:
            self.assertIn(path, reason)
        self.assertIn("further writers are blocked", reason)
        self.assertIn("no retry approval was created", reason)
        self.assertIn("stage input identity %s" % attempts.current_stage_input_identity(self.task_dir, stage), reason)
        self.assertIn("a new allowance requires a change to its consumed inputs", reason)
        self.assertNotIn("approve-retry", reason)


class F03LiveExhaustionTests(F03Base):
    def test_f03_fr1_budget1_failed_writer_blocks_without_approval_and_names_path(self):
        self.use_budget(1)
        self.fail_stage5_writer()

        code, state = self.pipeline()

        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(state["state"], "blocked")
        self.assertIsNone(state.get("pending_approval"))
        self.assertEqual(self.dispatches("05"), 1)
        self.assertEqual(state["last_failure"]["stage"], "05")
        self.assert_exhaustion_reason(state["last_failure"]["reason"], "05", TOUCHED)
        self.assertIn("failed_writer_blocked_without_allowance", self.log_events())
        self.assertNotIn("failed_writer_retry_approval_required", self.log_events())
        self.assertNotIn("approval_consumed", self.log_events())
        # Never reset or revert user files.
        self.assertTrue((self.root / TOUCHED).exists())

        # There is nothing to approve, and approve-retry says so.
        code, output = self.approve("retry-anything")
        self.assertEqual(code, EXIT_BAD_INPUT)
        self.assertIn("no pending approval", output)

    def test_f03_fr1_uncertain_source_comparison_names_the_uncertainty(self):
        state = new_state(self.task, "f03-uncertain")
        failed = self.stage_result("02", "invalid\n", FAILURE_CLASS_MAX_TURNS)
        failed["execution_mode"] = "workspace-write"
        failed["_source_before_error"] = "git status failed: fixture"

        code, calls = self.run_stage_with_results(
            state, [failed], stage_key="02", execution_mode="workspace-write",
            config=dict(self.config(), stage_attempt_budget=1),
        )

        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(len(calls), 1)
        self.assertEqual(state["state"], "blocked")
        self.assertIsNone(state.get("pending_approval"))
        reason = state["last_failure"]["reason"]
        self.assertIn("changed paths are uncertain", reason)
        self.assertIn("git status failed: fixture", reason)
        self.assertIn("further writers are blocked", reason)
        self.assertIn("a new allowance requires a change to its consumed inputs", reason)
        persisted = load_state(self.task_dir, self.task)
        self.assertIsNone(persisted.get("pending_approval"))

    def test_f03_budget2_first_failure_still_leads_to_approval_and_a_second_writer(self):
        self.use_budget(2)
        self.fail_stage5_writer()

        code, state = self.pipeline()

        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(state["state"], "awaiting_retry_approval")
        pending = state["pending_approval"]
        self.assertEqual(pending["retry_type"], "failed_write_source_change")
        self.assertIn(TOUCHED, [item["path"] for item in pending["failed_source_changes"]])
        self.assertEqual(self.dispatches("05"), 1)

        code, _ = self.approve(pending["approval_id"])
        self.assertEqual(code, EXIT_SUCCESS)
        self.unset("FAKE_FAIL_STAGE", "FAKE_WRITER_TOUCH")
        code, state = self.pipeline()

        # The run proceeds to the ordinary human test checkpoint.
        self.assertEqual(state["state"], "awaiting_human_test", state.get("last_failure"))
        self.assertIsNone(state.get("pending_approval"))
        self.assertEqual(self.dispatches("05"), 2)
        self.assertIn("05", state["completed_stages"])
        second = self.dispatch_records("05")[-1]
        self.assertEqual(second["approval"]["approval_id"], pending["approval_id"])
        self.assertTrue(second["approval"]["consumed"])


class F03RecoveryTests(F03Base):
    def test_f03_fr1_recovery_does_not_recreate_approval_without_allowance(self):
        state = new_state(self.task, "f03-recover")
        dispatch, _ = self.durable_failed_writer(state)
        (self.root / "crash-partial.txt").write_text("changed before crash\n", encoding="utf-8")
        resumed = new_state(self.task, "f03-resume")

        with mock.patch.object(controller, "invoke_stage", side_effect=AssertionError("writer launched")):
            code = controller.ensure_real_stage(
                self.task_dir, resumed, dict(self.config(), stage_attempt_budget=1),
                "02", "workspace-write", {},
            )

        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(resumed["state"], "blocked")
        self.assertIsNone(resumed.get("pending_approval"))
        self.assert_exhaustion_reason(resumed["last_failure"]["reason"], "02", "crash-partial.txt")
        self.assertNotIn("durable recovery blocked", resumed["last_failure"]["reason"])
        persisted = load_state(self.task_dir, self.task)
        self.assertIsNone(persisted.get("pending_approval"))
        self.assertEqual(persisted["last_failure"]["reason"], resumed["last_failure"]["reason"])
        # F03-FR2: the failed writer stays unresolved in its durable records.
        attempt_dir = attempts.attempts_root(self.task_dir) / dispatch["attempt_id"]
        self.assertFalse((attempt_dir / "writer-resolution.json").exists())

    def test_f03_fr2_new_identity_recreates_source_bound_approval_before_any_writer(self):
        state = new_state(self.task, "f03-fr2")
        dispatch, _ = self.durable_failed_writer(state)
        (self.root / "crash-partial.txt").write_text("changed before crash\n", encoding="utf-8")
        cfg = dict(self.config(), stage_attempt_budget=1)
        with mock.patch.object(controller, "invoke_stage", side_effect=AssertionError("writer launched")):
            self.assertEqual(
                controller.ensure_real_stage(self.task_dir, new_state(self.task, "f03-fr2-a"), cfg, "02", "workspace-write", {}),
                EXIT_BLOCKED,
            )
            # A changed consumed input is a new identity with its own allowance.
            (self.task_dir / CONTRACTS["01"].filename).write_text(
                valid_artifact("01") + "\nRevised requirement.\n", encoding="utf-8"
            )
            resumed = new_state(self.task, "f03-fr2-b")
            code = controller.ensure_real_stage(self.task_dir, resumed, cfg, "02", "workspace-write", {})

        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(resumed["state"], "awaiting_retry_approval")
        pending = resumed["pending_approval"]
        self.assertEqual(pending["retry_type"], "failed_write_source_change")
        self.assertEqual(pending["failed_attempt_id"], dispatch["attempt_id"])
        self.assertFalse(pending["approved"])
        self.assertIn("crash-partial.txt", [item["path"] for item in pending["failed_source_changes"]])

    def test_f03_fr2_end_to_end_brief_change_requires_approval_before_writer(self):
        self.use_budget(1)
        self.fail_stage5_writer()
        code, state = self.pipeline()
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertIsNone(state.get("pending_approval"))
        failed_attempt = self.dispatch_records("05")[-1]["attempt_id"]
        gates_before = self.dispatches("04_gate")

        # Unchanged inputs: resume neither dispatches nor offers an approval.
        code, state = self.pipeline()
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertIsNone(state.get("pending_approval"))
        self.assert_exhaustion_reason(state["last_failure"]["reason"], "05", TOUCHED)
        self.assertEqual(self.dispatches("05"), 1)

        # Change the brief; the re-run gate yields a new Stage 5 identity.
        brief = self.task_dir / CONTRACTS["04"].filename
        brief.write_text(brief.read_text(encoding="utf-8") + "\nRevised brief.\n", encoding="utf-8")
        self.env(FAKE_SUFFIX_04_gate="\nRevised audit.\n")
        code, state = self.pipeline()

        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(self.dispatches("04_gate"), gates_before + 1)
        self.assertEqual(self.dispatches("05"), 1, "no writer may run before source-bound approval")
        self.assertEqual(state["state"], "awaiting_retry_approval")
        pending = state["pending_approval"]
        self.assertEqual(pending["retry_type"], "failed_write_source_change")
        self.assertEqual(pending["failed_attempt_id"], failed_attempt)

        code, _ = self.approve(pending["approval_id"])
        self.assertEqual(code, EXIT_SUCCESS)
        self.unset("FAKE_FAIL_STAGE", "FAKE_WRITER_TOUCH")
        code, state = self.pipeline()
        # The run proceeds to the ordinary human test checkpoint.
        self.assertEqual(state["state"], "awaiting_human_test", state.get("last_failure"))
        self.assertIsNone(state.get("pending_approval"))
        self.assertEqual(self.dispatches("05"), 2)
        self.assertTrue(self.dispatch_records("05")[-1]["approval"]["consumed"])


class F03ConsumptionTests(F03Base):
    def approved_state(self, retry_type="human_approved_full_stage_retry"):
        state = new_state(self.task, "f03-fr3")
        state["state"] = "awaiting_retry_approval"
        state["pending_approval"] = {
            "approval_id": "retry-f03", "stage": "02", "retry_type": retry_type,
            "failure_class": FAILURE_CLASS_MAX_TURNS, "approved": True, "consumed": False,
        }
        return state

    def test_f03_fr3_approval_stays_unconsumed_when_nothing_is_dispatched(self):
        state = self.approved_state()
        with mock.patch.object(controller, "choose_real_agent", return_value=None):
            code, calls = self.run_stage_with_results(state, [], stage_key="02")

        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(calls, [])
        self.assertFalse(state["pending_approval"]["consumed"])
        self.assertNotIn("approval_consumed", self.log_events())

        # The same approval is consumed by the step that dispatches.
        success = self.stage_result("02", valid_artifact("02"))
        code, calls = self.run_stage_with_results(state, [success], stage_key="02")
        self.assertEqual(code, EXIT_SUCCESS)
        self.assertEqual(len(calls), 1)
        self.assertIn("approval_consumed", self.log_events())
        self.assertLess(self.log_events().index("approval_consumed"), self.log_events().index("stage_dispatch"))

    def test_f03_fr3_exhausted_source_bound_approval_is_not_consumed(self):
        # The audit I2 route: approved, then ensure_real_stage finds no attempt.
        state = new_state(self.task, "f03-i2")
        dispatch, _ = self.durable_failed_writer(state)
        (self.root / "crash-partial.txt").write_text("changed before crash\n", encoding="utf-8")
        state["state"] = "awaiting_retry_approval"
        state["pending_approval"] = {
            "approval_id": "retry-i2", "stage": "02", "retry_type": "failed_write_source_change",
            "failure_class": FAILURE_CLASS_MAX_TURNS, "approved": True, "consumed": False,
            "failed_attempt_id": dispatch["attempt_id"], "failed_attempt_number": 1,
            "failed_source_changes": [{"path": "crash-partial.txt", "reason": "untracked"}],
            "approved_source_baseline": controller.capture_writer_source_baseline(self.task_dir),
        }

        with mock.patch.object(controller, "invoke_stage", side_effect=AssertionError("writer launched")):
            code = controller.ensure_real_stage(
                self.task_dir, state, dict(self.config(), stage_attempt_budget=1), "02", "workspace-write", {},
            )

        self.assertEqual(code, EXIT_BLOCKED)
        self.assertNotIn("approval_consumed", self.log_events())
        withdrawn = state["approval_history"][-1]
        self.assertEqual(withdrawn["approval_id"], "retry-i2")
        self.assertFalse(withdrawn["consumed"])


class F03WithdrawalTests(F03Base):
    def dead_approval(self, approved):
        """State left by 21d83c2: an approval created on the last attempt."""
        state = new_state(self.task, "f03-fr4")
        dispatch, _ = self.durable_failed_writer(state)
        (self.root / "crash-partial.txt").write_text("changed before crash\n", encoding="utf-8")
        controller.require_retry_approval(
            state, "02", FAILURE_CLASS_MAX_TURNS, "codex",
            "source changed after failed write-capable attempt",
            retry_type="failed_write_source_change",
            failed_attempt_number=1, failed_attempt_id=dispatch["attempt_id"],
            failed_source_changes=[{"path": "crash-partial.txt", "reason": "untracked"}],
        )
        if approved:
            state["pending_approval"]["approved"] = True
            state["pending_approval"]["approved_source_baseline"] = controller.capture_writer_source_baseline(self.task_dir)
        controller.write_state_atomic(self.task_dir, state)
        return state, dispatch

    def assert_withdrawn(self, state, approval_id):
        self.assertIsNone(state.get("pending_approval"))
        self.assertEqual(state["state"], "blocked")
        history = [item for item in state.get("approval_history", []) if item["approval_id"] == approval_id]
        self.assertEqual(len(history), 1)
        self.assertTrue(history[0]["withdrawn"])
        self.assertFalse(history[0]["consumed"])
        self.assertTrue(history[0]["withdrawn_reason"])
        self.assert_exhaustion_reason(state["last_failure"]["reason"], "02", "crash-partial.txt")

    def check_dead_approval_withdrawn_on_run(self, approved):
        state, _ = self.dead_approval(approved)
        approval_id = state["pending_approval"]["approval_id"]
        resumed = load_state(self.task_dir, self.task)

        with mock.patch.object(controller, "invoke_stage", side_effect=AssertionError("writer launched")):
            code = controller.ensure_real_stage(
                self.task_dir, resumed, dict(self.config(), stage_attempt_budget=1),
                "02", "workspace-write", {},
            )

        self.assertEqual(code, EXIT_BLOCKED)
        self.assert_withdrawn(resumed, approval_id)
        self.assert_withdrawn(load_state(self.task_dir, self.task), approval_id)
        self.assertIn("approval_withdrawn", self.log_events())

    def test_f03_fr4_unapproved_dead_approval_is_withdrawn_durably_on_run(self):
        self.check_dead_approval_withdrawn_on_run(approved=False)

    def test_f03_fr4_approved_dead_approval_is_withdrawn_durably_on_run(self):
        self.check_dead_approval_withdrawn_on_run(approved=True)

    def test_f03_fr4_approve_retry_withdraws_an_approval_with_no_attempt_left(self):
        self.use_budget(1)
        state, _ = self.dead_approval(approved=False)
        approval_id = state["pending_approval"]["approval_id"]

        code, output = self.approve(approval_id)

        self.assertEqual(code, EXIT_BLOCKED)
        self.assertIn("approval withdrawn", output)
        self.assertNotIn("approval granted", output)
        self.assert_withdrawn(load_state(self.task_dir, self.task), approval_id)
        self.assertNotIn("approval_granted", self.log_events())


class F03Stage7ReasonTests(F03Base):
    def test_f03_fr5_stage7_exhaustion_names_identity_and_never_suggests_approve_retry(self):
        (self.task_dir / CONTRACTS["06"].filename).write_text(valid_artifact("06"), encoding="utf-8")
        state = new_state(self.task, "f03-fr5")
        identity = "7" * 64
        prompt = self.task_dir / "f03-review.prompt.md"
        prompt.write_text("review\n", encoding="utf-8")
        dispatch = attempts.prepare_dispatch(
            self.task_dir, state, "07", "claude", "read-only", 1, 1, "normal", "test",
            prompt, {}, None, {"model": "fake"}, review_input_identity=identity,
        )
        failed = self.stage_result("07", "invalid\n", "malformed_artifact")
        failed["attempt_id"] = dispatch["attempt_id"]
        attempts.record_completion(self.task_dir, failed, "invalid\n", {"valid": False}, False)

        with mock.patch.object(controller, "invoke_stage", side_effect=AssertionError("review launched")):
            code = controller.ensure_real_stage(
                self.task_dir, state, dict(self.config(), stage_attempt_budget=1), "07", "read-only", {},
                review_input_identity=identity,
            )

        self.assertEqual(code, EXIT_BLOCKED)
        reason = state["last_failure"]["reason"]
        self.assertIn("review-input identity %s" % identity, reason)
        self.assertIn("a new allowance requires a source, review-input, review-config, or bound Stage 6 change", reason)
        self.assertNotIn("approve-retry", reason)
        self.assertNotIn("approv", reason)
        self.assertIsNone(state.get("pending_approval"))


class F03AutomaticAcceptanceGuardTests(F03Base):
    def test_f03_a1_invalid_evidence_stays_ineligible_and_valid_path_accepts(self):
        handoff = {"route": "manual_test"}
        invalid = (
            (dict(self.config(), enable_auto_verified=False), self.verification_report(overall_status="passed", coverage_status="ok", driven_project_verified=True)),
            (self.config(), self.verification_report(overall_status="failed", coverage_status="ok", driven_project_verified=False)),
            (self.config(), self.verification_report(overall_status="passed", coverage_status="flagged", driven_project_verified=True)),
            (self.config(), self.verification_report()),
        )
        for config, report in invalid:
            with self.subTest(report=report):
                eligibility = controller.automatic_verification_eligibility(config, report, handoff)
                self.assertFalse(eligibility["eligible"])
                self.assertTrue(eligibility["reason"])

        # Budget 1: a valid run reaches automatic acceptance without any
        # approval being created, withdrawn, or consumed.
        self.use_budget(1)
        controller.verification.run_verification = lambda *args, **kwargs: self.verification_report(
            overall_status="passed", coverage_status="ok", driven_project_verified=True
        )
        code, state = self.pipeline()
        self.assertEqual(code, EXIT_SUCCESS)
        self.assertEqual(state["state"], "complete")
        self.assertIn("- [x] Accept", (self.task_dir / CONTRACTS["08"].filename).read_text(encoding="utf-8"))
        self.assertIsNone(state.get("pending_approval"))
        self.assertFalse(state.get("approval_history"))
        events = self.log_events()
        for event in ("approval_consumed", "approval_withdrawn", "failed_writer_blocked_without_allowance"):
            self.assertNotIn(event, events)


if __name__ == "__main__":
    unittest.main()
