from __future__ import print_function

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from agent_pipeline import controller
from agent_pipeline.failures import EXIT_LOCKED
from agent_pipeline.locking import (
    ExecutionOwnership,
    LockError,
    TaskLock,
    WorktreeLock,
    canonical_git_worktree,
    explicit_unlock,
    lock_path,
    pid_live,
    worktree_lock_path,
)
from agent_pipeline.state import orchestrator_dir


class LockingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.task_dir = Path(self.tmp.name) / "lock-test"

    def tearDown(self):
        self.tmp.cleanup()

    def write_lock(self, pid):
        root = orchestrator_dir(self.task_dir)
        root.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": pid,
            "host": socket.gethostname(),
            "started_at": "2099-01-01T00:00:00Z",
            "command": "test",
            "run_id": "existing",
        }
        lock_path(self.task_dir).write_text(json.dumps(payload) + "\n", encoding="utf-8")

    def test_lock_acquire_and_release(self):
        with TaskLock(self.task_dir, "test", "run-1"):
            self.assertTrue(lock_path(self.task_dir).exists())
        self.assertFalse(lock_path(self.task_dir).exists())

    def test_existing_active_lock_blocks(self):
        with TaskLock(self.task_dir, "test", "run-1"):
            with self.assertRaises(LockError) as raised:
                with TaskLock(self.task_dir, "other", "run-2"):
                    pass
        self.assertIn("explicit unlock required", str(raised.exception))
        self.assertFalse(raised.exception.unlockable)

    def test_atomic_open_race_raises_lock_error(self):
        def race(*args, **kwargs):
            self.write_lock(os.getpid())
            raise FileExistsError()

        with patch("agent_pipeline.locking.os.open", side_effect=race):
            with self.assertRaises(LockError) as raised:
                with TaskLock(self.task_dir, "test", "run-1"):
                    pass

        self.assertIn("explicit unlock required", str(raised.exception))
        self.assertFalse(raised.exception.unlockable)

    def test_stale_lock_diagnostic_marks_unlockable(self):
        self.write_lock(999999999)

        with self.assertRaises(LockError) as raised:
            with TaskLock(self.task_dir, "other", "run-2"):
                pass
        self.assertIn("PID is not live", str(raised.exception))
        self.assertTrue(raised.exception.unlockable)

    def test_explicit_unlock_refuses_live_lock(self):
        self.write_lock(os.getpid())

        result = explicit_unlock(self.task_dir, "operator requested")

        self.assertFalse(result["unlocked"])
        self.assertIn("still live", result["message"])
        self.assertTrue(lock_path(self.task_dir).exists())

    def test_explicit_unlock_archives_stale_lock(self):
        self.write_lock(999999999)

        result = explicit_unlock(self.task_dir, "operator requested")

        self.assertTrue(result["unlocked"])
        self.assertFalse(lock_path(self.task_dir).exists())
        archives = list((orchestrator_dir(self.task_dir) / "runs").glob("lock-unlocked-*.json"))
        self.assertEqual(len(archives), 1)
        archived = json.loads(archives[0].read_text(encoding="utf-8"))
        self.assertEqual(archived["unlock_reason"], "operator requested")

    def test_pid_live_distinguishes_permission_dead_and_uncertain(self):
        with patch("agent_pipeline.locking.os.kill", return_value=None):
            self.assertTrue(pid_live(123))
        with patch("agent_pipeline.locking.os.kill", side_effect=PermissionError()):
            self.assertTrue(pid_live(123))
        with patch("agent_pipeline.locking.os.kill", side_effect=ProcessLookupError()):
            self.assertFalse(pid_live(123))
        with patch("agent_pipeline.locking.os.kill", side_effect=OSError()):
            self.assertIsNone(pid_live(123))
        self.assertIsNone(pid_live("not-a-pid"))
        self.assertIsNone(pid_live(0))
        self.assertIsNone(pid_live(-1))


class WorktreeOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        (self.root / ".git").mkdir()
        self.tasks = self.root / ".agent-pipeline" / "tasks"
        self.task_a = self.tasks / "task-a"
        self.task_b = self.tasks / "task-b"

    def tearDown(self):
        self.tmp.cleanup()

    def test_c04_fr1_subprocess_write_write_and_write_verify_are_exclusive(self):
        holder_code = (
            "import sys, time\n"
            "from pathlib import Path\n"
            "from agent_pipeline.locking import ExecutionOwnership\n"
            "root, task_dir, task, command = sys.argv[1:]\n"
            "with ExecutionOwnership(Path(task_dir), Path(root), command, 'holder-run', task):\n"
            " print('READY', flush=True)\n"
            " input()\n"
        )
        package_root = str(Path(__file__).resolve().parents[2])
        env = os.environ.copy()
        env["PYTHONPATH"] = package_root + os.pathsep + env.get("PYTHONPATH", "")

        for holder_command, competitor_command in (("run", "run"), ("run", "verify")):
            with self.subTest(holder=holder_command, competitor=competitor_command):
                proc = subprocess.Popen(
                    [sys.executable, "-c", holder_code, str(self.root), str(self.task_a), "task-a", holder_command],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )
                try:
                    self.assertEqual(proc.stdout.readline().strip(), "READY")
                    with self.assertRaises(LockError) as raised:
                        with ExecutionOwnership(self.task_b, self.root, competitor_command, "competitor-run", "task-b"):
                            self.fail("competing source-dependent operation started")
                    message = str(raised.exception)
                    self.assertIn("task=task-a", message)
                    self.assertIn("run=holder-run", message)
                    self.assertIn("pid=%s" % proc.pid, message)
                finally:
                    if proc.poll() is None:
                        proc.stdin.write("\n")
                        proc.stdin.flush()
                    proc.communicate(timeout=5)

    def test_c04_fr2_aliases_share_one_key_and_distinct_worktrees_do_not(self):
        nested = self.root / "src" / "pkg"
        nested.mkdir(parents=True)
        alias = Path(self.tmp.name) / "repo-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(canonical_git_worktree(nested), self.root.resolve())
        self.assertEqual(canonical_git_worktree(alias / "src"), self.root.resolve())

        other = Path(self.tmp.name) / "other-worktree"
        other.mkdir()
        (other / ".git").mkdir()
        with WorktreeLock(self.root, "run", "run-a", "task-a"):
            with self.assertRaises(LockError):
                with WorktreeLock(alias / "src", "verify", "run-b", "task-b"):
                    pass
            with WorktreeLock(other, "verify", "run-c", "task-c"):
                self.assertTrue(worktree_lock_path(other).exists())

    def test_c04_fr3_atomic_race_and_consistent_lock_order(self):
        events = []
        original_task_enter = TaskLock.__enter__
        original_worktree_enter = WorktreeLock.__enter__

        def task_enter(lock):
            events.append("task")
            return original_task_enter(lock)

        def worktree_enter(lock):
            events.append("worktree")
            return original_worktree_enter(lock)

        with patch.object(TaskLock, "__enter__", task_enter), patch.object(WorktreeLock, "__enter__", worktree_enter):
            with ExecutionOwnership(self.task_a, self.root, "run", "ordered-run", "task-a"):
                pass
        self.assertEqual(events, ["task", "worktree"])

        def race(*args, **kwargs):
            payload = {
                "pid": os.getpid(), "host": socket.gethostname(), "command": "verify",
                "run_id": "winner", "task": "task-b",
            }
            path = worktree_lock_path(self.root)
            path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            raise FileExistsError()

        with patch("agent_pipeline.locking.os.open", side_effect=race):
            with self.assertRaises(LockError) as raised:
                with WorktreeLock(self.root, "run", "loser", "task-a"):
                    pass
        self.assertIn("task=task-b", str(raised.exception))
        worktree_lock_path(self.root).unlink()

    def test_c04_fr3_unlock_refuses_live_or_uncertain_worktree_owner(self):
        self.task_a.mkdir(parents=True)
        task_payload = {
            "pid": 999999999, "host": socket.gethostname(), "command": "run",
            "run_id": "same-run", "task": "task-a",
        }
        lock_path(self.task_a).parent.mkdir(parents=True, exist_ok=True)
        lock_path(self.task_a).write_text(json.dumps(task_payload) + "\n", encoding="utf-8")
        execution_payload = dict(task_payload, pid=os.getpid())
        worktree_lock_path(self.root).write_text(json.dumps(execution_payload) + "\n", encoding="utf-8")

        result = explicit_unlock(self.task_a, "unsafe", repo_root=self.root)

        self.assertFalse(result["unlocked"])
        self.assertIn("still live", result["message"])
        self.assertTrue(lock_path(self.task_a).exists())
        self.assertTrue(worktree_lock_path(self.root).exists())

        execution_payload["pid"] = 12345
        worktree_lock_path(self.root).write_text(json.dumps(execution_payload) + "\n", encoding="utf-8")
        with patch("agent_pipeline.locking.pid_live", side_effect=[False, None]):
            result = explicit_unlock(self.task_a, "unsafe", repo_root=self.root)
        self.assertFalse(result["unlocked"])
        self.assertIn("uncertain", result["message"])
        self.assertTrue(worktree_lock_path(self.root).exists())

    def test_c04_fr3_unlock_can_archive_stale_worktree_owner_without_task_lock(self):
        payload = {
            "pid": 999999999, "host": socket.gethostname(), "command": "run",
            "run_id": "orphaned-run", "task": "task-a",
        }
        worktree_lock_path(self.root).parent.mkdir(parents=True, exist_ok=True)
        worktree_lock_path(self.root).write_text(json.dumps(payload) + "\n", encoding="utf-8")

        result = explicit_unlock(self.task_a, "recover orphaned ownership", repo_root=self.root)

        self.assertTrue(result["unlocked"])
        self.assertFalse(worktree_lock_path(self.root).exists())
        archives = list((orchestrator_dir(self.task_a) / "runs").glob("worktree-lock-unlocked-*.json"))
        self.assertEqual(len(archives), 1)

    def test_c04_fr2_controller_paths_and_background_launch_no_child_when_owned(self):
        old_repo_root = controller.REPO_ROOT
        old_tasks_root = controller.TASKS_ROOT
        controller.REPO_ROOT = self.root
        controller.TASKS_ROOT = self.tasks
        self.addCleanup(setattr, controller, "REPO_ROOT", old_repo_root)
        self.addCleanup(setattr, controller, "TASKS_ROOT", old_tasks_root)

        with ExecutionOwnership(self.task_a, self.root, "run", "holder", "task-a"):
            output = StringIO()
            with patch.object(controller, "load_config", return_value={"verification": {}}), \
                    patch.object(controller, "run_real_pipeline") as run_pipeline, \
                    redirect_stdout(output):
                self.assertEqual(controller.pipeline_run("task-b"), EXIT_LOCKED)
            run_pipeline.assert_not_called()

            with patch.object(controller, "load_config", return_value={"verification": {}}), \
                    patch.object(controller.verification, "run_verification") as run_verification, \
                    redirect_stdout(output):
                self.assertEqual(controller.pipeline_verify("task-b"), EXIT_LOCKED)
            run_verification.assert_not_called()

            with patch.object(controller.subprocess, "Popen") as popen, redirect_stdout(output):
                self.assertEqual(controller.launch_background("task-b", ["run", "task-b"], "background.log"), EXIT_LOCKED)
            popen.assert_not_called()
        self.assertIn("task=task-a", output.getvalue())

    def test_c05_fr3_abrupt_controller_death_keeps_live_orphan_blocked(self):
        child_pid_path = Path(self.tmp.name) / "managed-child.pid"
        worker_code = (
            "import sys\n"
            "from pathlib import Path\n"
            "from agent_pipeline.locking import ExecutionOwnership, record_managed_child\n"
            "from agent_pipeline.real_runner import run_to_files\n"
            "root, task_dir, pid_path = map(Path, sys.argv[1:])\n"
            "run_id = 'orphan-run'\n"
            "with ExecutionOwnership(task_dir, root, 'run', run_id, 'task-a'):\n"
            " def launched(identity):\n"
            "  record_managed_child(task_dir, root, run_id, identity)\n"
            " run_to_files([sys.executable, '-c', \"import os,time; open(%r, 'w').write(str(os.getpid())); time.sleep(60)\"], task_dir/'child.stdout', task_dir/'child.stderr', 60, run_id=run_id, on_launch_identity=launched)\n"
        ) % str(child_pid_path)
        package_root = str(Path(__file__).resolve().parents[2])
        env = os.environ.copy()
        env["PYTHONPATH"] = package_root + os.pathsep + env.get("PYTHONPATH", "")
        worker = subprocess.Popen(
            [sys.executable, "-c", worker_code, str(self.root), str(self.task_a), str(child_pid_path)],
            env=env,
        )
        child_pid = None
        try:
            deadline = time.time() + 5
            while time.time() < deadline and not child_pid_path.exists():
                time.sleep(0.02)
            self.assertTrue(child_pid_path.exists())
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            task_record = json.loads(lock_path(self.task_a).read_text(encoding="utf-8"))
            worktree_record = json.loads(worktree_lock_path(self.root).read_text(encoding="utf-8"))
            for record in (task_record, worktree_record):
                self.assertEqual(record["managed_child"]["pid"], child_pid)
                self.assertEqual(record["managed_child"]["pgid"], child_pid)
                self.assertEqual(record["managed_child"]["run_id"], "orphan-run")
                self.assertIsNotNone(record["managed_child"]["process_start_identity"])

            worker.kill()
            worker.wait(timeout=5)
            with self.assertRaises(LockError) as raised:
                with ExecutionOwnership(self.task_b, self.root, "run", "competitor", "task-b"):
                    self.fail("orphan ownership was bypassed")
            self.assertIn("managed child is still live", str(raised.exception))
            result = explicit_unlock(self.task_a, "unsafe orphan unlock", repo_root=self.root)
            self.assertFalse(result["unlocked"])
            self.assertIn("managed child is still live", result["message"])
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.wait()
            if child_pid is not None:
                try:
                    os.killpg(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_c05_fr3_incomplete_child_identity_blocks_and_pid_reuse_is_not_killed(self):
        self.task_a.mkdir(parents=True)
        incomplete = {
            "pid": 999999999,
            "host": socket.gethostname(),
            "command": "run",
            "run_id": "incomplete-run",
            "task": "task-a",
            "managed_child": {"pid": 123, "pgid": 123, "host": socket.gethostname(), "run_id": "incomplete-run"},
        }
        lock_path(self.task_a).parent.mkdir(parents=True, exist_ok=True)
        lock_path(self.task_a).write_text(json.dumps(incomplete) + "\n", encoding="utf-8")
        result = explicit_unlock(self.task_a, "must remain blocked")
        self.assertFalse(result["unlocked"])
        self.assertIn("incomplete", result["message"])

        complete = dict(incomplete)
        complete["managed_child"] = dict(
            incomplete["managed_child"],
            process_start_identity={"scheme": "linux-proc-stat-v1", "boot_id": "old", "start_ticks": "1"},
        )
        lock_path(self.task_a).write_text(json.dumps(complete) + "\n", encoding="utf-8")
        with patch("agent_pipeline.locking.pid_live", side_effect=[False, True]), \
                patch("agent_pipeline.locking.process_group_live", return_value=False), \
                patch("agent_pipeline.locking.process_start_identity", return_value={"scheme": "linux-proc-stat-v1", "boot_id": "new", "start_ticks": "2"}), \
                patch("agent_pipeline.locking.os.killpg") as killpg:
            result = explicit_unlock(self.task_a, "PID was positively identified as reused")
        self.assertTrue(result["unlocked"])
        killpg.assert_not_called()


if __name__ == "__main__":
    unittest.main()
