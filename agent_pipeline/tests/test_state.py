from __future__ import print_function

import json
import tempfile
import unittest
from pathlib import Path

from agent_pipeline.mock_agent import valid_artifact
from agent_pipeline.state import (
    CorruptState,
    CONTRACTS,
    STAGE_ORDER,
    load_state,
    new_state,
    reconcile_artifacts,
    state_path,
    write_state_atomic,
)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.task = "state-test"
        self.task_dir = Path(self.tmp.name) / self.task
        self.task_dir.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write_artifact(self, stage_key, text=None):
        contract = CONTRACTS[stage_key]
        path = self.task_dir / contract.filename
        path.write_text(text if text is not None else valid_artifact(stage_key), encoding="utf-8")
        return path

    def test_state_creation_and_loading(self):
        state = new_state(self.task, "run-1")
        write_state_atomic(self.task_dir, state)

        loaded = load_state(self.task_dir, self.task)
        self.assertEqual(loaded["task"], self.task)
        self.assertEqual(loaded["run_id"], "run-1")
        self.assertEqual(loaded["state"], "ready")

    def test_schema_v1_state_is_migrated_in_memory(self):
        state = new_state(self.task, "run-1")
        for key in (
            "real_stage_runs",
            "stage_gate_passes",
            "stage_agents",
            "execution_modes",
            "manifest",
            "overseer",
            "fallback_history",
            "dirty_baseline",
            "next_required_human_action",
        ):
            state.pop(key)
        state["schema_version"] = 1
        path = state_path(self.task_dir)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(state) + "\n", encoding="utf-8")

        loaded = load_state(self.task_dir, self.task)

        self.assertEqual(loaded["schema_version"], 2)
        self.assertEqual(loaded["real_stage_runs"], {})
        self.assertEqual(loaded["stage_gate_passes"], [])

    def test_corrupt_state_is_rejected(self):
        state_file = state_path(self.task_dir)
        state_file.parent.mkdir(parents=True)
        state_file.write_text("{not-json\n", encoding="utf-8")

        with self.assertRaises(CorruptState):
            load_state(self.task_dir, self.task)

        state_file.write_text(json.dumps({"schema_version": 1}) + "\n", encoding="utf-8")
        with self.assertRaises(CorruptState):
            load_state(self.task_dir, self.task)

    def test_read_only_reconciliation_does_not_create_state_file(self):
        self.write_artifact("00")
        state = new_state(self.task)

        reconcile_artifacts(self.task_dir, state, read_only=True)

        self.assertFalse(state_path(self.task_dir).exists())
        self.assertEqual(state["completed_stages"], ["00"])
        self.assertEqual(state["current_stage"], "01")

    def test_stale_artifact_invalidates_downstream_completion(self):
        for key in STAGE_ORDER:
            self.write_artifact(key)
        state = new_state(self.task)
        reconcile_artifacts(self.task_dir, state)
        self.assertIsNone(state["current_stage"])

        self.write_artifact("02", valid_artifact("02") + "\nChanged input.\n")
        invalidated = reconcile_artifacts(self.task_dir, state, read_only=True)

        self.assertIn("03", invalidated)
        self.assertIn("02", state["completed_stages"])
        self.assertNotIn("03", state["completed_stages"])
        self.assertTrue(state["artifact_status"][CONTRACTS["03"].filename]["stale"])
        self.assertEqual(state["current_stage"], "03")

    def test_resume_point_calculation(self):
        for key in ("00", "01", "02"):
            self.write_artifact(key)
        state = new_state(self.task)
        reconcile_artifacts(self.task_dir, state)
        self.assertEqual(state["current_stage"], "03")

        for key in STAGE_ORDER[3:]:
            self.write_artifact(key)
        reconcile_artifacts(self.task_dir, state)
        self.assertIsNone(state["current_stage"])
        self.assertEqual(state["state"], "complete")

    def test_completed_stages_are_contiguous_dependency_prefix(self):
        for key in ("00", "01", "02", "04", "04_gate", "05"):
            self.write_artifact(key)
        self.write_artifact("03", "# Stage 3 - Specification audit\n\nInvalid body.\n")
        state = new_state(self.task)

        reconcile_artifacts(self.task_dir, state, read_only=True)

        self.assertEqual(state["completed_stages"], ["00", "01", "02"])
        self.assertEqual(state["current_stage"], "03")
        self.assertEqual(state["artifact_status"][CONTRACTS["04"].filename]["status"], "valid")
        self.assertEqual(state["artifact_status"][CONTRACTS["04_gate"].filename]["status"], "valid")
        self.assertEqual(state["artifact_status"][CONTRACTS["05"].filename]["status"], "valid")


if __name__ == "__main__":
    unittest.main()
