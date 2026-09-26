"""R01 regressions: recovery respects consumed inputs and invalidation."""

from __future__ import print_function

import json
import io
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from agent_pipeline import attempts, controller
from agent_pipeline.artifacts import sha256_file
from agent_pipeline.failures import EXIT_SUCCESS
from agent_pipeline.mock_agent import valid_artifact
from agent_pipeline.runner import atomic_finalize
from agent_pipeline.state import CONTRACTS, load_state, new_state
from agent_pipeline.tests import test_real_pipeline as base


class R01RecoveryTests(unittest.TestCase):
    setUp = base.RealPipelineTests.setUp
    tearDown = base.RealPipelineTests.tearDown
    config = base.RealPipelineTests.config
    write_fake_agent = base.RealPipelineTests.write_fake_agent
    verification_report = base.RealPipelineTests.verification_report

    def dispatch(self, state, stage="02", attempt_number=1):
        prompt = self.task_dir / ("r01-%s-%s.prompt.md" % (stage, attempt_number))
        prompt.write_text("prompt\n", encoding="utf-8")
        return attempts.prepare_dispatch(
            self.task_dir, state, stage, "codex", "read-only", 1,
            attempt_number, "normal", "initial/no-retry", prompt,
            {"deliberately": "stale"}, state.get("dirty_baseline"),
            {"model": "fake", "mode": "read-only"},
            review_input_identity=("0" * 64 if stage == "07" else None),
        )

    def complete(self, dispatch, output=None):
        stage = dispatch["stage"]
        output = output or valid_artifact(stage)
        candidate = self.task_dir / (dispatch["attempt_id"] + ".candidate.md")
        metadata = self.task_dir / (dispatch["attempt_id"] + ".json")
        candidate.write_text(output, encoding="utf-8")
        metadata.write_text('{"status": "passed"}\n', encoding="utf-8")
        result = {
            "attempt_id": dispatch["attempt_id"], "stage": stage,
            "run_id": dispatch["run_id"], "agent": "codex", "provider": "codex",
            "execution_mode": dispatch["execution_mode"], "pass_number": 1,
            "attempt_number": dispatch["attempt_number"], "attempt_kind": "normal",
            "retry_reason": "initial/no-retry", "candidate_artifact_path": str(candidate),
            "metadata_path": str(metadata), "stdout_path": str(candidate),
            "stderr_path": str(metadata), "exit_code": 0, "failure_class": None,
            "status": "passed", "postcondition": {"valid": True},
        }
        attempts.record_completion(self.task_dir, result, output, {"valid": True}, True)
        return result

    def promote(self, dispatch, output=None):
        output = output or valid_artifact(dispatch["stage"])
        result = self.complete(dispatch, output)
        final = atomic_finalize(self.task_dir, dispatch["stage"], output, read_only=True)
        self.assertTrue(final["finalized"])
        attempts.record_promotion(self.task_dir, result, Path(final["path"]))
        return result

    def test_r01_fr1_dispatch_hashes_live_consumed_artifacts(self):
        state = new_state(self.task, "r01-fr1")
        state["input_hashes"] = {CONTRACTS["00"].filename: "old-acknowledgement"}

        dispatch = self.dispatch(state)

        expected = {
            CONTRACTS[stage].filename: sha256_file(self.task_dir / CONTRACTS[stage].filename)
            for stage in ("00", "01")
        }
        self.assertEqual(dispatch["consumed_input_hashes"], expected)
        self.assertEqual(dispatch["input_hashes"], expected)

    def test_r01_fr2_changed_input_is_stale_and_not_acknowledged(self):
        state = new_state(self.task, "r01-fr2")
        dispatch = self.dispatch(state)
        self.complete(dispatch)
        original_hash = dispatch["consumed_input_hashes"][CONTRACTS["00"].filename]
        (self.task_dir / CONTRACTS["00"].filename).write_text(
            "# Original request\n\nChanged after dispatch.\n", encoding="utf-8"
        )
        current_hash = sha256_file(self.task_dir / CONTRACTS["00"].filename)

        resumed = new_state(self.task, "resume-fr2")
        self.assertIsNone(attempts.recover(self.task_dir, resumed, atomic_finalize, stages=("02",)))

        self.assertFalse((self.task_dir / CONTRACTS["02"].filename).exists())
        self.assertNotIn("02", resumed["real_stage_runs"])
        self.assertNotEqual(resumed["input_hashes"].get(CONTRACTS["00"].filename), current_hash)
        self.assertNotEqual(original_hash, current_hash)

    def test_r01_fr1_fr2_request_edit_revokes_acceptance_and_redispatches_stage02(self):
        count_path = self.root / "counts.txt"
        os.environ["FAKE_COUNT_PATH"] = str(count_path)
        self.addCleanup(lambda: os.environ.pop("FAKE_COUNT_PATH", None))
        controller.verification.run_verification = lambda *args, **kwargs: self.verification_report(
            overall_status="passed", coverage_status="ok", driven_project_verified=True
        )
        self.assertEqual(controller.pipeline_run(self.task, allow_dirty=True), EXIT_SUCCESS)
        before = count_path.read_text(encoding="utf-8").splitlines().count("02")

        request_path = self.task_dir / CONTRACTS["00"].filename
        request_path.write_text("# Original request\n\nChanged accepted request.\n", encoding="utf-8")
        status_output = io.StringIO()
        with redirect_stdout(status_output):
            controller.status(self.task)
        self.assertIn("current_acceptance: no", status_output.getvalue())

        self.assertEqual(controller.pipeline_run(self.task, allow_dirty=True), EXIT_SUCCESS)
        after = count_path.read_text(encoding="utf-8").splitlines().count("02")
        self.assertEqual(after, before + 1)

    def test_r01_fr2_invalid_result_evidence_blocks_recovery(self):
        state = new_state(self.task, "r01-fr2-invalid")
        dispatch = self.dispatch(state)
        self.complete(dispatch)
        result_path = attempts.attempts_root(self.task_dir) / dispatch["attempt_id"] / "result.md"
        result_path.write_text("tampered\n", encoding="utf-8")

        reason = attempts.recover(self.task_dir, new_state(self.task, "resume-invalid"), atomic_finalize, stages=("02",))

        self.assertIn(dispatch["attempt_id"], reason)
        self.assertIn("invalid or conflicting result evidence", reason)
        self.assertFalse((self.task_dir / CONTRACTS["02"].filename).exists())

    def test_r01_fr3_invalidated_promotion_is_never_resurrected(self):
        (self.task_dir / CONTRACTS["06"].filename).write_text(valid_artifact("06"), encoding="utf-8")
        state = new_state(self.task, "r01-fr3")
        dispatch = self.dispatch(state, stage="07")
        self.promote(dispatch)

        archived = controller.invalidate_evidence(self.task_dir, state, ["07"], "review inputs changed")

        invalidated_path = attempts.attempts_root(self.task_dir) / dispatch["attempt_id"] / "invalidated.json"
        self.assertEqual(archived, [CONTRACTS["07"].filename])
        self.assertTrue(invalidated_path.is_file())
        invalidated = json.loads(invalidated_path.read_text(encoding="utf-8"))
        self.assertEqual(invalidated["attempt_id"], dispatch["attempt_id"])
        self.assertIn("review inputs changed", invalidated["reason"])
        self.assertFalse((self.task_dir / CONTRACTS["07"].filename).exists())

        resumed = new_state(self.task, "resume-fr3")
        self.assertIsNone(attempts.recover(self.task_dir, resumed, atomic_finalize, stages=("07",)))
        self.assertFalse((self.task_dir / CONTRACTS["07"].filename).exists())
        self.assertNotIn("07", resumed["real_stage_runs"])

    def test_r01_fr4_later_promotion_supersedes_earlier_success(self):
        state = new_state(self.task, "r01-fr4")
        first = self.dispatch(state, attempt_number=1)
        first_output = valid_artifact("02") + "\nEarlier result.\n"
        self.promote(first, first_output)
        second = self.dispatch(state, attempt_number=2)
        second_output = valid_artifact("02") + "\nLater result.\n"
        self.promote(second, second_output)

        # Exercise recovery without relying on the canonical file to identify
        # which immutable promotion came later.
        (self.task_dir / CONTRACTS["02"].filename).unlink()
        resumed = new_state(self.task, "resume-fr4")
        self.assertIsNone(attempts.recover(self.task_dir, resumed, atomic_finalize, stages=("02",)))

        self.assertEqual((self.task_dir / CONTRACTS["02"].filename).read_text(encoding="utf-8"), second_output)
        runs = resumed["real_stage_runs"]["02"]
        self.assertEqual([run["attempt_id"] for run in runs], [second["attempt_id"]])

    def test_r01_fr5_legacy_success_without_provenance_blocks_by_attempt(self):
        state = new_state(self.task, "r01-fr5")
        dispatch = self.dispatch(state)
        self.complete(dispatch)
        dispatch_path = attempts.attempts_root(self.task_dir) / dispatch["attempt_id"] / "dispatch.json"
        legacy = json.loads(dispatch_path.read_text(encoding="utf-8"))
        legacy.pop("consumed_input_hashes")
        dispatch_path.write_text(json.dumps(legacy, sort_keys=True) + "\n", encoding="utf-8")

        reason = attempts.recover(self.task_dir, new_state(self.task, "resume-fr5"), atomic_finalize, stages=("02",))

        self.assertIn(dispatch["attempt_id"], reason)
        self.assertIn("lacks dispatch-time input provenance", reason)
        self.assertFalse((self.task_dir / CONTRACTS["02"].filename).exists())

    def test_r01_fr2_valid_success_is_adopted_once_and_auto_acceptance_still_works(self):
        state = new_state(self.task, "r01-valid-recovery")
        dispatch = self.dispatch(state)
        self.complete(dispatch)
        resumed = new_state(self.task, "resume-valid")

        self.assertIsNone(attempts.recover(self.task_dir, resumed, atomic_finalize, stages=("02",)))
        self.assertIsNone(attempts.recover(self.task_dir, resumed, atomic_finalize, stages=("02",)))
        self.assertEqual(len(resumed["real_stage_runs"]["02"]), 1)
        self.assertEqual(
            resumed["input_hashes"], dispatch["consumed_input_hashes"],
        )

        self.setUp_for_second_task("r01-valid-auto")
        controller.verification.run_verification = lambda *args, **kwargs: self.verification_report(
            overall_status="passed", coverage_status="ok", driven_project_verified=True
        )
        self.assertEqual(controller.pipeline_run(self.task, allow_dirty=True), EXIT_SUCCESS)
        accepted = load_state(self.task_dir, self.task)
        self.assertEqual(accepted["state"], "complete")
        self.assertTrue(accepted["evidence"]["verification"][-1]["source_current"])
        self.assertIn("- [x] Accept", (self.task_dir / CONTRACTS["08"].filename).read_text(encoding="utf-8"))

    def setUp_for_second_task(self, task):
        base.RealPipelineTests.setUp_for_second_task(self, task)


class R01AutomaticEvidenceGuardTests(unittest.TestCase):
    setUp = base.RealPipelineTests.setUp
    tearDown = base.RealPipelineTests.tearDown
    config = base.RealPipelineTests.config
    write_fake_agent = base.RealPipelineTests.write_fake_agent
    verification_report = base.RealPipelineTests.verification_report

    def test_r01_a1_invalid_automatic_evidence_remains_ineligible(self):
        handoff = {"route": "manual_test"}
        cases = (
            dict(self.config(), enable_auto_verified=False),
            self.config(),
        )
        reports = (
            self.verification_report(overall_status="passed", coverage_status="ok", driven_project_verified=True),
            self.verification_report(overall_status="failed", coverage_status="ok", driven_project_verified=False),
        )
        for config, report in zip(cases, reports):
            with self.subTest(config=config, report=report):
                eligibility = controller.automatic_verification_eligibility(config, report, handoff)
                self.assertFalse(eligibility["eligible"])
                self.assertTrue(eligibility["reason"])


if __name__ == "__main__":
    unittest.main()
