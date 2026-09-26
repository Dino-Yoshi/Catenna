"""R03 regressions for durable failed-writer retry approval."""

from __future__ import print_function

import subprocess
import unittest
from unittest import mock

from agent_pipeline import attempts, controller
from agent_pipeline.durable import DurableStorageError
from agent_pipeline.failures import EXIT_BLOCKED, EXIT_SUCCESS, FAILURE_CLASS_MAX_TURNS
from agent_pipeline.mock_agent import valid_artifact
from agent_pipeline.runner import atomic_finalize
from agent_pipeline.state import CONTRACTS, load_state, new_state
from agent_pipeline.tests import test_real_pipeline as base


class R03FailedWriterApprovalTests(unittest.TestCase):
    setUp = base.RealPipelineTests.setUp
    tearDown = base.RealPipelineTests.tearDown
    config = base.RealPipelineTests.config
    write_fake_agent = base.RealPipelineTests.write_fake_agent
    stage_result = base.RealPipelineTests.stage_result
    run_stage_with_results = base.RealPipelineTests.run_stage_with_results

    def git(self, *args):
        return subprocess.check_output(["git"] + list(args), cwd=str(self.root))

    def durable_failed_writer(self, state, baseline=None, output=None):
        prompt = self.task_dir / "r03-prompt.md"
        prompt.write_text("failed writer prompt\n", encoding="utf-8")
        state.setdefault("attempts", {})["02"] = 1
        dispatch = attempts.prepare_dispatch(
            self.task_dir, state, "02", "codex", "workspace-write", 1, 1,
            "normal", "initial/no-retry", prompt, {},
            baseline if baseline is not None else controller.capture_writer_source_baseline(self.task_dir),
            {"model": "fake", "mode": "workspace-write"},
        )
        result = self.stage_result(
            "02", output if output is not None else valid_artifact("02"),
            FAILURE_CLASS_MAX_TURNS, attempt_number=1,
        )
        result.update({
            "attempt_id": dispatch["attempt_id"],
            "dispatch_path": str(attempts.attempts_root(self.task_dir) / dispatch["attempt_id"] / "dispatch.json"),
            "execution_mode": "workspace-write",
            "postcondition": {"valid": True},
        })
        attempts.record_completion(
            self.task_dir, result, output if output is not None else valid_artifact("02"),
            {"valid": True}, False,
        )
        return dispatch, result

    def test_r03_fr1_approval_is_durable_before_return_and_storage_failure_stops(self):
        state = new_state(self.task, "run-r03-fr1")
        failed = self.stage_result("02", "invalid\n", FAILURE_CLASS_MAX_TURNS)
        failed["execution_mode"] = "workspace-write"
        failed["_source_before"] = controller.capture_writer_source_baseline(self.task_dir)
        (self.root / "writer-created.txt").write_text("partial\n", encoding="utf-8")

        code, calls = self.run_stage_with_results(
            state, [failed], stage_key="02", execution_mode="workspace-write",
            config=dict(self.config(), stage_attempt_budget=2),
        )

        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(len(calls), 1)
        persisted = load_state(self.task_dir, self.task)
        self.assertEqual(persisted["state"], "awaiting_retry_approval")
        self.assertEqual(persisted["pending_approval"]["retry_type"], "failed_write_source_change")

        state = new_state(self.task, "run-r03-fr1-storage")
        failed = self.stage_result("02", "invalid\n", FAILURE_CLASS_MAX_TURNS)
        failed["execution_mode"] = "workspace-write"
        failed["_source_before"] = controller.capture_writer_source_baseline(self.task_dir)
        (self.root / "writer-created-2.txt").write_text("partial\n", encoding="utf-8")
        real_write = controller.write_state_atomic

        def fail_approval_write(task_dir, candidate_state):
            if candidate_state.get("pending_approval"):
                raise DurableStorageError("injected approval persistence failure")
            return real_write(task_dir, candidate_state)

        with mock.patch.object(controller, "write_state_atomic", side_effect=fail_approval_write):
            with self.assertRaises(DurableStorageError):
                self.run_stage_with_results(
                    state, [failed], stage_key="02", execution_mode="workspace-write",
                    config=dict(self.config(), stage_attempt_budget=2),
                )
        self.assertEqual(state["attempts"]["02"], 1)

    def test_r03_fr2_invalid_failed_evidence_recreates_approval_and_launches_nothing(self):
        # F03-FR4: approve-retry checks the same stage budget as the run, so
        # the operator-facing config must match the budget-2 run below.
        controller.load_config = lambda: dict(self.config(), stage_attempt_budget=2)
        state = new_state(self.task, "run-r03-fr2")
        baseline = controller.capture_writer_source_baseline(self.task_dir)
        dispatch, _result = self.durable_failed_writer(state, baseline=baseline)
        (self.root / "crash-partial.txt").write_text("changed before crash\n", encoding="utf-8")
        resumed = new_state(self.task, "resume-r03-fr2")
        launched = []

        with mock.patch.object(controller, "invoke_stage", side_effect=lambda *a, **k: launched.append(True)):
            code = controller.ensure_real_stage(
                self.task_dir, resumed, dict(self.config(), stage_attempt_budget=2),
                "02", "workspace-write", {},
            )

        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(launched, [])
        self.assertEqual(resumed["state"], "awaiting_retry_approval")
        self.assertEqual(resumed["pending_approval"]["failed_attempt_id"], dispatch["attempt_id"])
        persisted = load_state(self.task_dir, self.task)
        self.assertEqual(persisted["pending_approval"]["failed_attempt_id"], dispatch["attempt_id"])

        approval_id = persisted["pending_approval"]["approval_id"]
        self.assertEqual(controller.approve_retry(self.task, approval_id), EXIT_SUCCESS)
        approved = load_state(self.task_dir, self.task)
        success = self.stage_result("02", valid_artifact("02"), attempt_number=2)
        success["execution_mode"] = "workspace-write"
        code, calls = self.run_stage_with_results(
            approved, [success], stage_key="02", execution_mode="workspace-write",
            config=dict(self.config(), stage_attempt_budget=2),
        )
        self.assertEqual(code, EXIT_SUCCESS, approved)
        self.assertEqual(len(calls), 1)
        self.assertEqual(approved["attempts"]["02"], 2)

    def test_r03_fr2_valid_success_still_recovers_automatically(self):
        state = new_state(self.task, "run-r03-fr2-valid")
        prompt = self.task_dir / "r03-valid-prompt.md"
        prompt.write_text("valid writer prompt\n", encoding="utf-8")
        state["attempts"]["02"] = 1
        dispatch = attempts.prepare_dispatch(
            self.task_dir, state, "02", "codex", "workspace-write", 1, 1,
            "normal", "initial/no-retry", prompt, {},
            controller.capture_writer_source_baseline(self.task_dir),
            {"model": "fake", "mode": "workspace-write"},
        )
        result = self.stage_result("02", valid_artifact("02"))
        result.update({
            "attempt_id": dispatch["attempt_id"], "execution_mode": "workspace-write",
            "postcondition": {"valid": True},
        })
        attempts.record_completion(self.task_dir, result, valid_artifact("02"), {"valid": True}, True)
        resumed = new_state(self.task, "resume-r03-fr2-valid")

        reason = attempts.recover(
            self.task_dir, resumed, atomic_finalize, stages=("02",),
            failed_writer_recovery=lambda d, c: controller.recover_failed_writer_approval(
                self.task_dir, resumed, d, c
            ),
        )

        self.assertIsNone(reason)
        self.assertTrue((self.task_dir / CONTRACTS["02"].filename).is_file())
        self.assertIsNone(resumed.get("pending_approval"))

    def test_r03_fr3_complete_source_identity_detects_required_change_classes(self):
        self.git("config", "user.email", "r03@example.com")
        self.git("config", "user.name", "R03 Test")
        tracked = self.root / "tracked-r03.txt"
        tracked.write_text("base\n", encoding="utf-8")
        self.git("add", tracked.name)
        self.git("commit", "-m", "r03 baseline")

        tracked.write_text("dirty one\n", encoding="utf-8")
        baseline = controller.capture_writer_source_baseline(self.task_dir)
        tracked.write_text("dirty two\n", encoding="utf-8")
        changes, error = controller.writer_source_changes(self.task_dir, baseline)
        self.assertIsNone(error)
        self.assertTrue(any(item["path"] == tracked.name for item in changes))

        baseline = controller.capture_writer_source_baseline(self.task_dir)
        tracked.chmod(tracked.stat().st_mode | 0o100)
        changes, error = controller.writer_source_changes(self.task_dir, baseline)
        self.assertIsNone(error)
        self.assertIn(
            {"path": tracked.name, "reason": "executable_mode_changed_during_stage5"},
            changes,
        )

        baseline = controller.capture_writer_source_baseline(self.task_dir)
        self.git("add", tracked.name)
        changes, error = controller.writer_source_changes(self.task_dir, baseline)
        self.assertIsNone(error)
        self.assertTrue(any(item["reason"] == "index_changed_during_failed_writer" for item in changes))

        baseline = controller.capture_writer_source_baseline(self.task_dir)
        self.git("commit", "-m", "r03 head change")
        changes, error = controller.writer_source_changes(self.task_dir, baseline)
        self.assertIsNone(error)
        self.assertTrue(any(item["path"] == "<git-head>" for item in changes))

        baseline = controller.capture_writer_source_baseline(self.task_dir)
        self.git("mv", tracked.name, "renamed-r03.txt")
        changes, error = controller.writer_source_changes(self.task_dir, baseline)
        self.assertIsNone(error)
        paths = {item["path"] for item in changes}
        self.assertTrue({tracked.name, "renamed-r03.txt"} & paths)

        baseline = controller.capture_writer_source_baseline(self.task_dir)
        (self.root / "renamed-r03.txt").unlink()
        changes, error = controller.writer_source_changes(self.task_dir, baseline)
        self.assertIsNone(error)
        self.assertTrue(changes)

    def test_r03_fr4_unchanged_failure_retries_without_resetting_baseline_or_count(self):
        state = new_state(self.task, "run-r03-fr4")
        original_implementation_baseline = {"sentinel": "original implementation baseline"}
        state["dirty_baseline"] = original_implementation_baseline
        dispatch, _failed = self.durable_failed_writer(state)
        resumed = new_state(self.task, "resume-r03-fr4")
        resumed["dirty_baseline"] = original_implementation_baseline
        success = self.stage_result("02", valid_artifact("02"), attempt_number=2)
        success["execution_mode"] = "workspace-write"

        code, calls = self.run_stage_with_results(
            resumed, [success], stage_key="02", execution_mode="workspace-write",
            config=dict(self.config(), stage_attempt_budget=2),
        )

        self.assertEqual(code, EXIT_SUCCESS, resumed)
        self.assertEqual(len(calls), 1)
        self.assertEqual(resumed["attempts"]["02"], 2)
        self.assertEqual(resumed["dirty_baseline"], original_implementation_baseline)
        self.assertIsNone(resumed.get("pending_approval"))
        self.assertTrue(
            (attempts.attempts_root(self.task_dir) / dispatch["attempt_id"] / "writer-resolution.json").is_file()
        )


if __name__ == "__main__":
    unittest.main()
