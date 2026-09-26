"""C08 regressions: durable dispatch, promotion, and crash recovery."""

from __future__ import print_function

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from agent_pipeline import attempts, controller, durable
from agent_pipeline.artifacts import sha256_file
from agent_pipeline.durable import DurableStorageError
from agent_pipeline.failures import EXIT_SUCCESS
from agent_pipeline.mock_agent import valid_artifact
from agent_pipeline.real_runner import run_to_files
from agent_pipeline.runner import atomic_finalize
from agent_pipeline.state import CONTRACTS, load_state, new_state, state_path, write_state_atomic
from agent_pipeline.tests import test_real_pipeline as base


class C08DurableRecoveryTests(unittest.TestCase):
    setUp = base.RealPipelineTests.setUp
    tearDown = base.RealPipelineTests.tearDown
    config = base.RealPipelineTests.config
    write_fake_agent = base.RealPipelineTests.write_fake_agent
    verification_report = base.RealPipelineTests.verification_report

    def dispatch(self, state, stage="02", attempt_number=1):
        prompt = self.task_dir / ("prompt-%s.md" % attempt_number)
        prompt.write_text("prompt %s\n" % attempt_number, encoding="utf-8")
        return attempts.prepare_dispatch(
            self.task_dir, state, stage, "codex", "read-only", 1,
            attempt_number, "normal", "initial/no-retry", prompt,
            {"00_original_request.md": "input-hash"}, state.get("dirty_baseline"),
            {"model": "fake", "mode": "read-only"},
        )

    def completed_result(self, dispatch, stage="02"):
        candidate = self.task_dir / (dispatch["attempt_id"] + ".candidate.md")
        candidate.write_text(valid_artifact(stage), encoding="utf-8")
        metadata = self.task_dir / (dispatch["attempt_id"] + ".json")
        metadata.write_text('{"status": "passed"}\n', encoding="utf-8")
        return {
            "attempt_id": dispatch["attempt_id"], "stage": stage,
            "run_id": dispatch["run_id"], "agent": "codex", "provider": "codex",
            "execution_mode": dispatch["execution_mode"], "pass_number": 1,
            "attempt_number": dispatch["attempt_number"], "attempt_kind": "normal",
            "retry_reason": "initial/no-retry", "candidate_artifact_path": str(candidate),
            "metadata_path": str(metadata), "stdout_path": str(candidate),
            "stderr_path": str(metadata), "exit_code": 0, "failure_class": None,
            "status": "passed", "postcondition": {"valid": True},
        }

    def test_fr1_failed_pre_dispatch_persistence_launches_no_child(self):
        state = new_state(self.task, "run-fr1")
        launched = []
        with mock.patch.object(attempts, "write_json_exclusive", side_effect=DurableStorageError("disk full")):
            with mock.patch.object(controller, "invoke_agent", side_effect=lambda *a, **k: launched.append(True)):
                with self.assertRaises(DurableStorageError):
                    controller.invoke_stage(self.task_dir, state, self.config(), "02", "read-only", "codex", 1)
        self.assertEqual(launched, [])

    def test_fr2_unique_attempt_paths_and_recoverable_promotion(self):
        state = new_state(self.task, "same-run")
        state["attempts"]["02"] = 1
        first = self.dispatch(state, attempt_number=1)
        state["attempts"]["02"] = 2
        second = self.dispatch(state, attempt_number=2)
        self.assertNotEqual(first["attempt_id"], second["attempt_id"])
        self.assertNotEqual(first["prompt_path"], second["prompt_path"])
        self.assertTrue(Path(first["prompt_path"]).exists())

        # Make the first a retained failed attempt and the second a proven
        # success whose controller crashed before canonical promotion.
        failed = self.completed_result(first)
        failed["postcondition"] = {"valid": False}
        attempts.record_completion(self.task_dir, failed, valid_artifact("02"), {"valid": True}, True)
        result = self.completed_result(second)
        attempts.record_completion(self.task_dir, result, valid_artifact("02"), {"valid": True}, True)
        canonical = self.task_dir / CONTRACTS["02"].filename
        self.assertFalse(canonical.exists())

        recovered = new_state(self.task, "resume-run")
        self.assertIsNone(attempts.recover(self.task_dir, recovered, atomic_finalize, stages=("02",)))
        self.assertTrue(canonical.exists())
        self.assertEqual(recovered["real_stage_runs"]["02"][-1]["attempt_id"], second["attempt_id"])
        self.assertTrue((attempts.attempts_root(self.task_dir) / second["attempt_id"] / "promoted.json").exists())

    def test_fr3_invalid_evidence_blocks_but_valid_success_is_adopted_once(self):
        state = new_state(self.task, "run-fr3")
        state["attempts"]["02"] = 1
        dispatch = self.dispatch(state)
        result = self.completed_result(dispatch)
        completed = attempts.record_completion(self.task_dir, result, valid_artifact("02"), {"valid": True}, True)
        Path(completed["result_path"]).write_text("tampered\n", encoding="utf-8")
        reason = attempts.recover(self.task_dir, new_state(self.task, "resume"), atomic_finalize, stages=("02",))
        self.assertIn("invalid or conflicting result evidence", reason)
        self.assertFalse((self.task_dir / CONTRACTS["02"].filename).exists())

        Path(completed["result_path"]).write_text(valid_artifact("02"), encoding="utf-8")
        resumed = new_state(self.task, "resume")
        self.assertIsNone(attempts.recover(self.task_dir, resumed, atomic_finalize, stages=("02",)))
        count = len(resumed["real_stage_runs"]["02"])
        self.assertIsNone(attempts.recover(self.task_dir, resumed, atomic_finalize, stages=("02",)))
        self.assertEqual(len(resumed["real_stage_runs"]["02"]), count)

    def test_fr4_recovery_retains_attempt_and_consumed_approval(self):
        state = new_state(self.task, "run-fr4")
        state["attempts"]["02"] = 4
        state["pending_approval"] = {"approval_id": "retry-fixed", "stage": "02", "approved": True, "consumed": True}
        dispatch = self.dispatch(state, attempt_number=4)
        failed = self.completed_result(dispatch)
        failed["exit_code"] = 1
        failed["failure_class"] = "unknown_failure"
        failed["status"] = "failed"
        attempts.record_completion(self.task_dir, failed, "invalid\n", {"valid": False}, False)

        resumed = new_state(self.task, "resume")
        resumed["pending_approval"] = {"approval_id": "retry-fixed", "stage": "02", "approved": True, "consumed": False}
        self.assertIsNone(attempts.recover(self.task_dir, resumed, atomic_finalize, stages=("02",)))
        self.assertEqual(resumed["attempts"]["02"], 4)
        self.assertTrue(resumed["pending_approval"]["consumed"])

    def test_fr5_replace_failure_retains_state_and_launch_record_failure_reaps_child(self):
        state = new_state(self.task, "stable")
        write_state_atomic(self.task_dir, state)
        before = state_path(self.task_dir).read_bytes()
        changed = dict(state)
        changed["state"] = "complete"
        with mock.patch.object(durable.os, "replace", side_effect=OSError("injected ENOSPC")):
            with self.assertRaises(DurableStorageError):
                write_state_atomic(self.task_dir, changed)
        self.assertEqual(state_path(self.task_dir).read_bytes(), before)

        stdout = self.task_dir / "child.stdout"
        stderr = self.task_dir / "child.stderr"
        child = {}
        def fail_record(identity):
            child.update(identity)
            raise DurableStorageError("injected fsync failure")
        with self.assertRaises(DurableStorageError):
            run_to_files(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdout, stderr, 30, on_launch_identity=fail_record, run_id="run-fr5",
            )
        with self.assertRaises(ProcessLookupError):
            os.kill(int(child["pid"]), 0)

    def test_fr1_fr5_valid_durable_path_reaches_automatic_acceptance(self):
        controller.verification.run_verification = lambda *args, **kwargs: self.verification_report(
            overall_status="passed", coverage_status="ok", driven_project_verified=True
        )
        self.assertEqual(controller.pipeline_run(self.task, allow_dirty=True), EXIT_SUCCESS)
        state = load_state(self.task_dir, self.task)
        self.assertEqual(state["state"], "complete")
        self.assertIn("- [x] Accept", (self.task_dir / CONTRACTS["08"].filename).read_text(encoding="utf-8"))
        durable_records = list(attempts.attempts_root(self.task_dir).glob("*/promoted.json"))
        self.assertGreaterEqual(len(durable_records), 6)
        for path in durable_records:
            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(sha256_file(Path(record["final_path"])), record["final_hash"])


if __name__ == "__main__":
    unittest.main()
