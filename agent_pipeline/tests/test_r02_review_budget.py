"""R02 regressions: one durable Stage 7 budget per review-input identity."""

from __future__ import print_function

import copy
import hashlib
import io
import json
import os
import unittest
from contextlib import redirect_stdout

from agent_pipeline import attempts, controller, evidence
from agent_pipeline.artifacts import sha256_file
from agent_pipeline.failures import EXIT_BLOCKED, EXIT_SUCCESS
from agent_pipeline.state import CONTRACTS, load_state
from agent_pipeline.tests import test_real_pipeline as base


class R02ReviewBudgetTests(unittest.TestCase):
    setUp = base.RealPipelineTests.setUp
    tearDown = base.RealPipelineTests.tearDown
    config = base.RealPipelineTests.config
    write_fake_agent = base.RealPipelineTests.write_fake_agent
    verification_report = base.RealPipelineTests.verification_report

    def enable_valid_automatic_acceptance(self):
        controller.verification.run_verification = lambda *args, **kwargs: self.verification_report(
            overall_status="passed", coverage_status="ok", driven_project_verified=True
        )

    def accepted_task(self):
        self.enable_valid_automatic_acceptance()
        self.assertEqual(controller.pipeline_run(self.task, allow_dirty=True), EXIT_SUCCESS)
        return load_state(self.task_dir, self.task)

    def test_r02_fr1_fr4_identity_is_bound_to_dispatch_evidence_status_and_report(self):
        state = self.accepted_task()
        record = state["evidence"]["stage07"][-1]
        review_identity = record["review_input_identity"]
        self.assertEqual(len(review_identity), 64)
        source, error = controller.current_source_identity(self.task_dir)
        self.assertIsNone(error)
        payload = {
            "source_fingerprint": source["fingerprint"],
            "review_inputs": evidence.subset(
                evidence.current_inputs(self.task_dir, self.config()), evidence.REVIEW_INPUT_KEYS
            ),
            "stage06_artifact_hash": sha256_file(self.task_dir / CONTRACTS["06"].filename),
        }
        expected = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()
        self.assertEqual(review_identity, expected)

        dispatches = []
        for directory in attempts.attempts_root(self.task_dir).iterdir():
            path = directory / "dispatch.json"
            if path.is_file():
                dispatch = json.loads(path.read_text(encoding="utf-8"))
                if dispatch.get("stage") == "07":
                    dispatches.append(dispatch)
        self.assertEqual(len(dispatches), 1)
        self.assertEqual(dispatches[0]["review_input_identity"], review_identity)

        status_output = io.StringIO()
        with redirect_stdout(status_output):
            self.assertEqual(controller.status(self.task), EXIT_SUCCESS)
        self.assertIn("review_input_identity: " + review_identity, status_output.getvalue())
        self.assertIn("review_attempts: 1/1", status_output.getvalue())

        report_output = io.StringIO()
        with redirect_stdout(report_output):
            self.assertEqual(controller.pipeline_report(self.task), EXIT_SUCCESS)
        self.assertIn("Review-input identity: " + review_identity, report_output.getvalue())
        self.assertIn("Review attempts: 1/1 used", report_output.getvalue())
        structured = json.loads((self.task_dir / ".orchestrator" / "task_report.json").read_text(encoding="utf-8"))
        self.assertEqual(structured["review_attempts"]["identity"], review_identity)
        self.assertEqual(structured["review_attempts"]["attempts_used"], 1)
        self.assertEqual(structured["review_attempts"]["attempts_allowed"], 1)

    def test_r02_fr2_fr3_failed_identity_is_not_refilled_and_new_identity_gets_one_attempt(self):
        self.accepted_task()
        count_path = self.root / "counts.txt"
        os.environ["FAKE_COUNT_PATH"] = str(count_path)
        os.environ["FAKE_FAIL_STAGE"] = "07"
        self.addCleanup(lambda: os.environ.pop("FAKE_COUNT_PATH", None))
        self.addCleanup(lambda: os.environ.pop("FAKE_FAIL_STAGE", None))

        changed = copy.deepcopy(self.config())
        changed["turn_budgets"]["07"] = 6
        controller.load_config = lambda: changed
        self.assertEqual(controller.pipeline_run(self.task, allow_dirty=True), EXIT_BLOCKED)
        after_first = count_path.read_text(encoding="utf-8").splitlines().count("07")
        self.assertEqual(after_first, 1)
        failed_state = load_state(self.task_dir, self.task)
        failed_identity = failed_state["evidence"]["stage07"][-1]["review_input_identity"]
        invalidated = [record for record in failed_state["evidence"]["stage07"] if record.get("status") == "invalidated"]
        self.assertTrue(invalidated)
        self.assertTrue(all(record.get("review_input_identity") for record in invalidated))

        self.assertEqual(controller.pipeline_run(self.task, allow_dirty=True), EXIT_BLOCKED)
        after_repeat = count_path.read_text(encoding="utf-8").splitlines().count("07")
        self.assertEqual(after_repeat, after_first)
        exhausted = load_state(self.task_dir, self.task)
        self.assertIn(failed_identity, exhausted["last_failure"]["reason"])
        # F03-FR5: the exhaustion reason names the identity and how a new
        # allowance is obtained, never approve-retry.
        self.assertNotIn("approve-retry", exhausted["last_failure"]["reason"])
        self.assertIn("review-config", exhausted["last_failure"]["reason"])

        newer = copy.deepcopy(changed)
        newer["turn_budgets"]["07"] = 7
        controller.load_config = lambda: newer
        self.assertEqual(controller.pipeline_run(self.task, allow_dirty=True), EXIT_BLOCKED)
        after_new_identity = count_path.read_text(encoding="utf-8").splitlines().count("07")
        self.assertEqual(after_new_identity, after_repeat + 1)
        newer_state = load_state(self.task_dir, self.task)
        self.assertNotEqual(newer_state["evidence"]["stage07"][-1]["review_input_identity"], failed_identity)

    def test_r02_fr2_crash_after_dispatch_does_not_refund_identity(self):
        state = self.accepted_task()
        summary = controller.current_evidence_summary(self.task_dir, state)["review_attempts"]
        prompt = self.task_dir / "r02-crash.prompt.md"
        prompt.write_text("review prompt\n", encoding="utf-8")
        dispatch = attempts.prepare_dispatch(
            self.task_dir, state, "07", "claude", "read-only", 1,
            state["attempts"]["07"] + 1, "normal", "new dispatch", prompt,
            state.get("input_hashes"), state.get("dirty_baseline"),
            {"model": "fake", "mode": "read-only"},
            review_input_identity=summary["identity"],
        )

        self.assertFalse((attempts.attempts_root(self.task_dir) / dispatch["attempt_id"] / "completed.json").exists())
        self.assertEqual(
            attempts.count_stage7_attempts(self.task_dir, summary["identity"]),
            summary["attempts_used"] + 1,
        )

    def test_r02_a1_invalid_evidence_stays_ineligible_and_valid_path_accepts(self):
        handoff = {"route": "manual_test"}
        invalid = (
            (dict(self.config(), enable_auto_verified=False), self.verification_report(overall_status="passed", coverage_status="ok", driven_project_verified=True)),
            (self.config(), self.verification_report(overall_status="failed", coverage_status="ok", driven_project_verified=False)),
            (self.config(), self.verification_report(overall_status="passed", coverage_status="flagged", driven_project_verified=True)),
        )
        for config, report in invalid:
            with self.subTest(report=report):
                self.assertFalse(controller.automatic_verification_eligibility(config, report, handoff)["eligible"])

        state = self.accepted_task()
        self.assertEqual(state["state"], "complete")
        self.assertTrue(state["evidence"]["verification"][-1]["source_current"])
        self.assertIn("- [x] Accept", (self.task_dir / CONTRACTS["08"].filename).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
