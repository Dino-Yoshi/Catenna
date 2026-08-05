from __future__ import print_function

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from agent_pipeline import controller
from agent_pipeline import usage
from agent_pipeline.failures import EXIT_SUCCESS
from agent_pipeline.mock_agent import valid_artifact
from agent_pipeline.runner import atomic_finalize
from agent_pipeline.state import CONTRACTS, load_state, new_state, reconcile_artifacts, state_path, write_state_atomic


class ControllerReliabilityTests(unittest.TestCase):
    def with_tasks_root(self, root):
        original = controller.TASKS_ROOT
        controller.TASKS_ROOT = root
        self.addCleanup(lambda: setattr(controller, "TASKS_ROOT", original))

    def with_usage_root(self, root):
        original = controller.USAGE_ROOT
        controller.USAGE_ROOT = root
        self.addCleanup(lambda: setattr(controller, "USAGE_ROOT", original))

    def capture_dry_run(self, task):
        output = io.StringIO()
        with redirect_stdout(output):
            code = controller.dry_run(task)
        return code, output.getvalue()

    def capture_status(self, task):
        output = io.StringIO()
        with redirect_stdout(output):
            code = controller.status(task)
        return code, output.getvalue()

    def test_dry_run_reports_artifact_status_without_creating_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.with_tasks_root(root)

            code, output = self.capture_dry_run("dry-run-missing")

            self.assertEqual(code, EXIT_SUCCESS)
            self.assertIn("would_resume_at: 00", output)
            self.assertIn("artifact_status:", output)
            self.assertIn("00_original_request.md: stage=00 status=missing reason=missing", output)
            self.assertFalse(state_path(root / "dry-run-missing").exists())

    def test_dry_run_reports_stale_downstream_without_rewriting_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.with_tasks_root(root)
            task = "dry-run-stale"
            task_dir = root / task
            task_dir.mkdir(parents=True)
            for stage_key in ("00", "01", "02", "03"):
                (task_dir / CONTRACTS[stage_key].filename).write_text(valid_artifact(stage_key), encoding="utf-8")
            state = new_state(task)
            reconcile_artifacts(task_dir, state)
            write_state_atomic(task_dir, state)
            before = state_path(task_dir).read_text(encoding="utf-8")

            (task_dir / CONTRACTS["02"].filename).write_text(valid_artifact("02") + "\nChanged.\n", encoding="utf-8")
            code, output = self.capture_dry_run(task)
            after = state_path(task_dir).read_text(encoding="utf-8")

            self.assertEqual(code, EXIT_SUCCESS)
            self.assertEqual(before, after)
            self.assertIn("would_resume_at: 03", output)
            self.assertIn("03_audit.md: stage=03 status=valid reason=valid stale=true", output)
            self.assertIn("stale_downstream_stages: 03", output)

    def test_dry_run_reports_prefix_completion_with_downstream_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.with_tasks_root(root)
            task = "dry-run-prefix"
            task_dir = root / task
            task_dir.mkdir(parents=True)
            for stage_key in ("00", "01", "02", "04", "04_gate", "05"):
                (task_dir / CONTRACTS[stage_key].filename).write_text(valid_artifact(stage_key), encoding="utf-8")
            (task_dir / CONTRACTS["03"].filename).write_text(
                "# Stage 3 - Specification audit\n\nInvalid body.\n",
                encoding="utf-8",
            )
            state = new_state(task)
            write_state_atomic(task_dir, state)
            before = state_path(task_dir).read_text(encoding="utf-8")

            code, output = self.capture_dry_run(task)
            after = state_path(task_dir).read_text(encoding="utf-8")

            self.assertEqual(code, EXIT_SUCCESS)
            self.assertEqual(before, after)
            self.assertIn("would_resume_at: 03", output)
            self.assertIn("completed_stages: 00, 01, 02", output)
            self.assertIn("04_final_codex_brief.md: stage=04 status=valid reason=valid", output)
            self.assertIn("04_final_brief_audit.md: stage=04_gate status=valid reason=valid", output)

    def test_mock_test_passes_against_committed_fixtures(self):
        # Guards against .agent-pipeline/fixtures/mock_scenarios.json drifting
        # out of sync with policies.py/controller.py again -- this fixture
        # drift previously went unnoticed by `unittest discover` entirely
        # because nothing here called controller.mock_test() directly (see
        # phase-3-handoff.md).
        output = io.StringIO()
        with redirect_stdout(output):
            code = controller.mock_test()
        self.assertEqual(code, EXIT_SUCCESS, output.getvalue())

    def test_checkpoint_noop_eligible_becomes_ineligible_once_stage6_is_valid(self):
        # Before Phase 3, a human hand-writing 06_manual_test_notes.md while
        # the controller sat at awaiting_human_test had no effect: the noop
        # check only ever looked at pre-Stage-6 artifact hashes. This is the
        # fix that lets a resumed pipeline-run actually drive Stage 7/8.
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "task"
            task_dir.mkdir(parents=True)
            state = {"state": "awaiting_human_test", "current_stage": "06", "human_checkpoint": {"noop_hashes": {}}}
            original = controller.stage5_postprocessing_complete
            controller.stage5_postprocessing_complete = lambda *args, **kwargs: {"valid": True}
            self.addCleanup(lambda: setattr(controller, "stage5_postprocessing_complete", original))

            result = controller.checkpoint_noop_eligible(task_dir, state)
            self.assertNotIn("Stage 6 manual test notes are ready", result["reason"])

            (task_dir / CONTRACTS["06"].filename).write_text(
                "# Stage 6 - Manual test notes\n\n## Decision\n\n- [x] Accept\n- [ ] Reject\n- [ ] Needs follow-up\n",
                encoding="utf-8",
            )
            result = controller.checkpoint_noop_eligible(task_dir, state)
            self.assertFalse(result["eligible"])
            self.assertIn("Stage 6 manual test notes are ready", result["reason"])

    def test_resume_reconciliation_skips_already_valid_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = "resume-reconcile"
            task_dir = Path(tmp) / task
            task_dir.mkdir(parents=True)
            for stage_key in ("00", "01", "02"):
                result = atomic_finalize(task_dir, stage_key, valid_artifact(stage_key))
                self.assertTrue(result["finalized"])
            state = new_state(task, "old-run")
            state["completed_stages"] = ["00", "01"]
            write_state_atomic(task_dir, state)
            loaded = load_state(task_dir, task)
            loaded["run_id"] = "run-test"

            code = controller.run_scenario(task_dir, task, loaded, {"actions": {}})

            self.assertEqual(code, EXIT_SUCCESS)
            self.assertIn("02", loaded["completed_stages"])
            self.assertEqual(loaded["agent_call_counts"].get("codex"), 3)

    def test_status_prints_active_cross_task_cooldowns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.with_tasks_root(root)
            self.with_usage_root(root / "usage")
            usage.record_cooldown(controller.cooldown_store_path(), "codex", "usage_limit", None, "other-task", "run-1", 900)

            code, output = self.capture_status("status-cooldown-task")

            self.assertEqual(code, EXIT_SUCCESS)
            self.assertIn("cross_task_cooldowns:", output)
            self.assertIn("codex", output)

    def test_status_prints_nothing_extra_when_no_cooldown_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.with_tasks_root(root)
            self.with_usage_root(root / "usage")

            code, output = self.capture_status("status-no-cooldown-task")

            self.assertEqual(code, EXIT_SUCCESS)
            self.assertNotIn("cross_task_cooldowns:", output)

    def test_status_never_raises_on_corrupt_cooldown_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.with_tasks_root(root)
            usage_root = root / "usage"
            self.with_usage_root(usage_root)
            usage_root.mkdir(parents=True)
            (usage_root / "agent_cooldowns.json").write_text("not json", encoding="utf-8")

            code, output = self.capture_status("status-corrupt-cooldown-task")

            self.assertEqual(code, EXIT_SUCCESS)
            self.assertNotIn("cross_task_cooldowns:", output)


if __name__ == "__main__":
    unittest.main()
