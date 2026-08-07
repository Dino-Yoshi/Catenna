from __future__ import print_function

import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_pipeline.locking import LockError, TaskLock, explicit_unlock, lock_path, pid_live
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

    def test_stale_lock_diagnostic_marks_unlockable(self):
        self.write_lock(999999999)

        with self.assertRaises(LockError) as raised:
            with TaskLock(self.task_dir, "other", "run-2"):
                pass
        self.assertIn("PID is not live", str(raised.exception))
        self.assertTrue(raised.exception.unlockable)

    def test_explicit_unlock_archives_lock(self):
        self.write_lock(os.getpid())

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


if __name__ == "__main__":
    unittest.main()
