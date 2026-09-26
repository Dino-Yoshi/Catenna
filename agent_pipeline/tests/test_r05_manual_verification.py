"""R05 regressions: configured checks bind manual Stage 6 acceptance."""

from __future__ import print_function

import copy
import io
import unittest
from contextlib import redirect_stdout

from agent_pipeline import controller, evidence
from agent_pipeline.failures import EXIT_BLOCKED, EXIT_SUCCESS, EXIT_VALIDATION
from agent_pipeline.state import CONTRACTS, load_state
from agent_pipeline.tests import test_real_pipeline as base


class R05ManualVerificationTests(unittest.TestCase):
    setUp = base.RealPipelineTests.setUp
    tearDown = base.RealPipelineTests.tearDown
    config = base.RealPipelineTests.config
    write_fake_agent = base.RealPipelineTests.write_fake_agent
    verification_report = base.RealPipelineTests.verification_report
    manual_notes = base.RealPipelineTests.manual_notes
    setUp_for_second_task = base.RealPipelineTests.setUp_for_second_task

    def start(self, checks=True, auto=False):
        self.cfg = self.config()
        self.cfg["enable_auto_verified"] = auto
        self.cfg["verification"]["driven_project_commands"] = (
            [{"name": "fixture", "argv": ["true"]}] if checks else []
        )
        controller.load_config = lambda: self.cfg
        self.verification_outcome = "failed"

        def fake_verification(*args, **kwargs):
            passed = self.verification_outcome == "passed"
            report = self.verification_report(
                overall_status="passed" if passed else "failed",
                coverage_status="ok",
                driven_project_verified=passed if checks else None,
            )
            report["report_paths"] = {
                "md_path": str(self.task_dir / "05_verification_report.md"),
                "json_path": str(self.task_dir / "05_verification_report.json"),
            }
            return report

        controller.verification.run_verification = fake_verification

    def pipeline(self):
        with redirect_stdout(io.StringIO()):
            return controller.pipeline_run(self.task, allow_dirty=True)

    def verify(self):
        with redirect_stdout(io.StringIO()):
            return controller.pipeline_verify(self.task)

    def output(self, action):
        stream = io.StringIO()
        with redirect_stdout(stream):
            action(self.task)
        return stream.getvalue()

    def state(self):
        return load_state(self.task_dir, self.task)

    def write_notes(self, decision="Accept"):
        (self.task_dir / CONTRACTS["06"].filename).write_text(
            self.manual_notes(decision=decision), encoding="utf-8"
        )

    def prepare_current_manual_acceptance(self):
        self.start(checks=True, auto=False)
        self.assertEqual(self.pipeline(), EXIT_BLOCKED)
        self.write_notes()
        self.verification_outcome = "passed"
        self.assertEqual(self.verify(), EXIT_SUCCESS)
        self.assertEqual(self.pipeline(), EXIT_SUCCESS)

    def assert_corrective_reason(self, reason, condition):
        self.assertIn("`fixture`", reason)
        self.assertIn(condition, reason)
        self.assertIn("fix the source and re-run `catenna verify`", reason)
        self.assertIn("record a non-accepting decision", reason)

    def test_r05_fr1_fr2_fr5_failed_check_blocks_binding_then_pass_allows_same_notes(self):
        self.start(checks=True, auto=False)
        self.assertEqual(self.pipeline(), EXIT_BLOCKED)
        self.write_notes()

        self.assertEqual(self.pipeline(), EXIT_BLOCKED)
        state = self.state()
        self.assertEqual(state["state"], "awaiting_human_test")
        self.assertEqual(state["evidence"]["stage06"], [])
        self.assertFalse((self.task_dir / CONTRACTS["07"].filename).exists())
        self.assert_corrective_reason(state["human_checkpoint"]["reason"], "failing verification evidence")
        self.assertIn("current_acceptance: no", self.output(controller.status))

        self.verification_outcome = "passed"
        self.assertEqual(self.verify(), EXIT_SUCCESS)
        self.assertEqual(self.pipeline(), EXIT_SUCCESS)
        self.assertEqual(self.state()["evidence"]["stage06"][-1]["route"], "manual")
        self.assertIn("current_acceptance: yes", self.output(controller.status))

    def test_r05_fr1_fr2_newer_failure_revokes_status_dry_run_report_and_continuation(self):
        self.prepare_current_manual_acceptance()
        self.verification_outcome = "failed"
        self.assertEqual(self.verify(), EXIT_VALIDATION)

        self.assertIn("current_acceptance: no", self.output(controller.status))
        self.assertIn("current_acceptance: no", self.output(controller.dry_run))
        report = self.output(controller.pipeline_report)
        self.assertIn("Current acceptance: **no**", report)
        self.assertEqual(self.pipeline(), EXIT_BLOCKED)
        self.assert_corrective_reason(self.state()["human_checkpoint"]["reason"], "failing verification evidence")

    def test_r05_fr2_fr5_invalid_manual_accept_evidence_is_named_and_actionable(self):
        self.prepare_current_manual_acceptance()
        accepted = self.state()
        identity, error = controller.current_source_identity(self.task_dir)
        self.assertIsNone(error)
        inputs = evidence.current_inputs(self.task_dir, self.cfg)

        cases = (
            ("missing", lambda state: state["evidence"].__setitem__("verification", []), "missing verification evidence"),
            ("failed", lambda state: state["evidence"]["verification"][-1].__setitem__("status", "failed"), "failing verification evidence"),
            ("interrupted", lambda state: state["evidence"]["verification"][-1].__setitem__("status", "interrupted"), "interrupted verification evidence"),
            ("unbound", self._make_verification_unbound, "unbound verification evidence"),
        )
        for label, mutate, condition in cases:
            with self.subTest(label=label):
                state = copy.deepcopy(accepted)
                mutate(state)
                summary = evidence.evaluate(self.task_dir, state, self.cfg, identity)
                self.assertFalse(summary["stage06"]["current"])
                self.assertFalse(summary["current_acceptance"])
                self.assert_corrective_reason(summary["stage06"]["reason"], condition)

    @staticmethod
    def _make_verification_unbound(state):
        record = state["evidence"]["verification"][-1]
        record["status"] = "passed"
        record["source_current"] = False
        record["source_reason"] = "configured check is not bound to source"

    def test_r05_fr2_final_boundary_rechecks_manual_acceptance(self):
        self.start(checks=True, auto=False)
        self.assertEqual(self.pipeline(), EXIT_BLOCKED)
        self.write_notes()
        self.verification_outcome = "passed"
        self.assertEqual(self.verify(), EXIT_SUCCESS)

        original = controller.ensure_stage08_decision

        def fail_verification_at_boundary(task_dir, state):
            code, decision = original(task_dir, state)
            latest = copy.deepcopy(state["evidence"]["verification"][-1])
            latest["status"] = "failed"
            evidence.append(state, "verification", latest)
            return code, decision

        controller.ensure_stage08_decision = fail_verification_at_boundary
        self.addCleanup(setattr, controller, "ensure_stage08_decision", original)

        self.assertEqual(self.pipeline(), EXIT_BLOCKED)
        reason = self.state()["last_failure"]["reason"]
        self.assertIn("final decision evidence is no longer current", reason)
        self.assert_corrective_reason(reason, "failing verification evidence")
        self.assertIn("current_acceptance: no", self.output(controller.status))

    def test_r05_fr3_reject_and_needs_followup_do_not_require_passing_checks(self):
        self.start(checks=True, auto=False)
        for index, decision in enumerate(("Reject", "Needs follow-up")):
            if index:
                self.setUp_for_second_task("r05-nonaccept-%d" % index)
            self.assertEqual(self.pipeline(), EXIT_BLOCKED)
            self.write_notes(decision)
            self.assertEqual(self.pipeline(), EXIT_VALIDATION)
            state = self.state()
            self.assertEqual(state["evidence"]["stage06"][-1]["route"], "manual")
            expected = "reject" if decision == "Reject" else "needs_followup"
            self.assertEqual(state["evidence"]["stage08"][-1]["decision"], expected)

    def test_r05_fr4_no_configured_checks_preserves_manual_only_acceptance(self):
        self.start(checks=False, auto=False)
        self.assertEqual(self.pipeline(), EXIT_BLOCKED)
        self.write_notes()
        self.assertEqual(self.pipeline(), EXIT_SUCCESS)
        self.assertEqual(self.state()["evidence"]["stage06"][-1]["route"], "manual")

    def test_r05_constraint_valid_automatic_acceptance_is_unchanged(self):
        self.start(checks=True, auto=True)
        self.verification_outcome = "passed"
        self.assertEqual(self.pipeline(), EXIT_SUCCESS)
        state = self.state()
        self.assertEqual(state["evidence"]["stage06"][-1]["route"], "auto")
        self.assertEqual(state["evidence"]["stage08"][-1]["decision"], "accept")


if __name__ == "__main__":
    unittest.main()
