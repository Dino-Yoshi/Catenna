"""Per-task lock handling."""

from __future__ import print_function

import json
import os
import socket
import time

from .state import orchestrator_dir


class LockError(Exception):
    def __init__(self, message, unlockable=False):
        Exception.__init__(self, message)
        self.unlockable = unlockable


def lock_path(task_dir):
    return orchestrator_dir(task_dir) / "lock.json"


def pid_live(pid):
    try:
        parsed = int(pid)
        if parsed < 1:
            return None
        os.kill(parsed, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return None


class TaskLock(object):
    def __init__(self, task_dir, command, run_id):
        self.task_dir = task_dir
        self.command = command
        self.run_id = run_id
        self.host = socket.gethostname()
        self.path = lock_path(task_dir)
        self.acquired = False

    def __enter__(self):
        directory = orchestrator_dir(self.task_dir)
        directory.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self._raise_existing()
        payload = {
            "pid": os.getpid(),
            "host": self.host,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "command": self.command,
            "run_id": self.run_id,
        }
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(str(self.path), flags, 0o644)
        except FileExistsError:
            self._raise_existing()
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        self.acquired = True
        return self

    def _raise_existing(self):
        try:
            with open(str(self.path), "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            raise LockError("lock exists but is unreadable; explicit unlock required", unlockable=False)
        if data.get("host") != self.host:
            raise LockError("lock is held by another host; explicit unlock required", unlockable=False)
        live = pid_live(data.get("pid"))
        if live is False:
            raise LockError("lock holder PID is not live on this host; explicit unlock may be used", unlockable=True)
        raise LockError("lock is active or liveness is uncertain; explicit unlock required", unlockable=False)

    def __exit__(self, exc_type, exc, tb):
        if self.acquired and self.path.exists():
            try:
                with open(str(self.path), "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if data.get("pid") == os.getpid() and data.get("run_id") == self.run_id:
                    os.unlink(str(self.path))
            finally:
                self.acquired = False


def explicit_unlock(task_dir, reason):
    path = lock_path(task_dir)
    if not path.exists():
        return {"unlocked": False, "message": "no lock exists"}
    try:
        with open(str(path), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        data = {"unreadable": True}
    archive = orchestrator_dir(task_dir) / "runs"
    archive.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    target = archive / ("lock-unlocked-%s.json" % stamp)
    data["unlock_reason"] = reason
    data["unlocked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(str(target), "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.unlink(str(path))
    return {"unlocked": True, "message": "lock archived to " + str(target)}
