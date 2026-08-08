from __future__ import print_function

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from agent_pipeline import controller
from agent_pipeline.failures import EXIT_BAD_INPUT, EXIT_SUCCESS


class CurrentTaskTests(unittest.TestCase):
    def with_repo_root(self, root):
        original = controller.REPO_ROOT
        controller.REPO_ROOT = root
        self.addCleanup(lambda: setattr(controller, "REPO_ROOT", original))

    def with_tasks_root(self, root):
        original = controller.TASKS_ROOT
        controller.TASKS_ROOT = root
        self.addCleanup(lambda: setattr(controller, "TASKS_ROOT", original))

    def test_read_current_task_unset_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.with_repo_root(Path(tmp))
            self.assertIsNone(controller.read_current_task())

    def test_write_then_read_current_task_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.with_repo_root(Path(tmp))
            controller.write_current_task("hardening-approve-retry")
            self.assertEqual(controller.read_current_task(), "hardening-approve-retry")

    def test_write_current_task_rejects_invalid_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.with_repo_root(Path(tmp))
            with self.assertRaises(controller.ControllerError):
                controller.write_current_task("../escape")

    def test_resolve_task_prefers_explicit_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.with_repo_root(Path(tmp))
            controller.write_current_task("pointer-task")
            task, used_default = controller.resolve_task("explicit-task")
            self.assertEqual(task, "explicit-task")
            self.assertFalse(used_default)

    def test_resolve_task_falls_back_to_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.with_repo_root(Path(tmp))
            controller.write_current_task("pointer-task")
            task, used_default = controller.resolve_task(None)
            self.assertEqual(task, "pointer-task")
            self.assertTrue(used_default)

    def test_resolve_task_raises_when_nothing_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.with_repo_root(Path(tmp))
            with self.assertRaises(controller.ControllerError) as ctx:
                controller.resolve_task(None)
            self.assertEqual(ctx.exception.exit_code, EXIT_BAD_INPUT)

    def test_use_task_sets_pointer_and_warns_on_missing_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.with_repo_root(root)
            self.with_tasks_root(root / ".agent-pipeline" / "tasks")

            output = io.StringIO()
            with redirect_stdout(output):
                code = controller.use_task("brand-new-task")

            self.assertEqual(code, EXIT_SUCCESS)
            self.assertIn("warning", output.getvalue())
            self.assertEqual(controller.read_current_task(), "brand-new-task")

    def test_use_task_with_no_argument_shows_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.with_repo_root(Path(tmp))
            controller.write_current_task("some-task")

            output = io.StringIO()
            with redirect_stdout(output):
                code = controller.use_task(None)

            self.assertEqual(code, EXIT_SUCCESS)
            self.assertIn("some-task", output.getvalue())

    def test_list_tasks_marks_current_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.with_repo_root(root)
            tasks_root = root / ".agent-pipeline" / "tasks"
            self.with_tasks_root(tasks_root)
            (tasks_root / "task-a").mkdir(parents=True)
            (tasks_root / "task-b").mkdir(parents=True)
            controller.write_current_task("task-b")

            output = io.StringIO()
            with redirect_stdout(output):
                code = controller.list_tasks()

            self.assertEqual(code, EXIT_SUCCESS)
            lines = output.getvalue().splitlines()
            self.assertTrue(any(line.startswith("*") and "task-b" in line for line in lines))
            self.assertTrue(any(not line.startswith("*") and "task-a" in line for line in lines))

    def test_list_tasks_plain_prints_bare_sorted_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.with_repo_root(root)
            tasks_root = root / ".agent-pipeline" / "tasks"
            self.with_tasks_root(tasks_root)
            (tasks_root / "task-b").mkdir(parents=True)
            (tasks_root / "task-a").mkdir(parents=True)
            controller.write_current_task("task-b")

            output = io.StringIO()
            with redirect_stdout(output):
                code = controller.list_tasks(plain=True)

            self.assertEqual(code, EXIT_SUCCESS)
            self.assertEqual(output.getvalue().splitlines(), ["task-a", "task-b"])

    def test_list_tasks_handles_missing_tasks_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.with_repo_root(root)
            self.with_tasks_root(root / "nonexistent" / "tasks")

            output = io.StringIO()
            with redirect_stdout(output):
                code = controller.list_tasks()

            self.assertEqual(code, EXIT_SUCCESS)
            self.assertIn("no tasks found", output.getvalue())


if __name__ == "__main__":
    unittest.main()
