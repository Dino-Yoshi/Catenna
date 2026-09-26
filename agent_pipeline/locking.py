"""Per-task and Git-worktree execution ownership."""

from __future__ import print_function

import json
import os
import socket
import sys
import time
from pathlib import Path

from .state import orchestrator_dir
from .durable import atomic_write_json, sync_directory


class LockError(Exception):
    def __init__(self, message, unlockable=False):
        Exception.__init__(self, message)
        self.unlockable = unlockable


def lock_path(task_dir):
    return orchestrator_dir(task_dir) / "lock.json"


def canonical_git_worktree(repo_root):
    """Return the physical top level of the Git worktree containing repo_root."""
    root = Path(repo_root).resolve()
    if root.is_file():
        root = root.parent
    for candidate in (root,) + tuple(root.parents):
        if (candidate / ".git").exists():
            return candidate.resolve()
    raise LockError("cannot identify canonical Git worktree from %s" % root, unlockable=False)


def worktree_lock_path(repo_root):
    return canonical_git_worktree(repo_root) / ".agent-pipeline" / "execution-lock.json"


def validate_execution_ownership(task_dir, repo_root, run_id, controller_pid=None):
    """Validate the mutable ownership records without requiring byte identity.

    Managed-child bookkeeping legitimately replaces both JSON files while a
    subprocess is active.  The stable authority is therefore the owning host,
    run, and controller PID, not a hash of the whole record.
    """
    expected_pid = os.getpid() if controller_pid is None else int(controller_pid)
    expected_host = socket.gethostname()
    records = (
        ("task lock", lock_path(task_dir)),
        ("worktree execution lock", worktree_lock_path(repo_root)),
    )
    observed = {}
    for name, path in records:
        path = Path(path)
        if not path.exists():
            raise LockError("%s %s is missing; execution ownership was lost" % (name, path), unlockable=False)
        try:
            with open(str(path), "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception as exc:
            raise LockError("%s %s is unreadable; execution ownership cannot be verified: %s" % (name, path, exc), unlockable=False)
        mismatches = []
        if data.get("host") != expected_host:
            mismatches.append("host=%r (expected %r)" % (data.get("host"), expected_host))
        if data.get("run_id") != run_id:
            mismatches.append("run_id=%r (expected %r)" % (data.get("run_id"), run_id))
        if data.get("pid") != expected_pid:
            mismatches.append("pid=%r (expected %r)" % (data.get("pid"), expected_pid))
        if mismatches:
            raise LockError("%s %s was replaced or changed owner: %s" % (name, path, ", ".join(mismatches)), unlockable=False)
        observed[name] = data
    return observed


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


def process_start_identity(pid):
    """Return the Linux kernel identity for a process, not merely its PID.

    A missing identity is deliberately represented as ``None``.  Recovery
    must then stay blocked rather than guessing that a reused PID is ours.
    """
    try:
        stat_text = Path("/proc/%d/stat" % int(pid)).read_text(encoding="utf-8")
        close_paren = stat_text.rfind(")")
        fields_after_comm = stat_text[close_paren + 2 :].split()
        start_ticks = fields_after_comm[19]
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        if not boot_id or not start_ticks:
            return None
        return {"scheme": "linux-proc-stat-v1", "boot_id": boot_id, "start_ticks": start_ticks}
    except Exception:
        return None


def process_group_live(pgid):
    try:
        parsed = int(pgid)
        if parsed < 1:
            return None
        os.killpg(parsed, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return None


def managed_child_status(data):
    """Classify a recorded child without treating a PID as an identity."""
    launch_state = data.get("managed_child_state")
    if launch_state == "launch_pending":
        return "uncertain", "managed child launch was not durably completed"
    if launch_state == "launch_failed":
        return "none", None
    child = data.get("managed_child")
    if not isinstance(child, dict):
        if launch_state == "launched":
            return "uncertain", "managed child launch record is incomplete"
        return "none", None
    required = ("pid", "pgid", "host", "process_start_identity", "run_id")
    if any(child.get(key) in (None, "") for key in required):
        return "uncertain", "managed child launch record is incomplete"
    if child.get("host") != socket.gethostname():
        return "uncertain", "managed child was launched on another host"

    live = pid_live(child.get("pid"))
    group_live = process_group_live(child.get("pgid"))
    if live is None or group_live is None:
        return "uncertain", "managed child or process-group liveness is uncertain"
    if live:
        current_identity = process_start_identity(child.get("pid"))
        if current_identity is None:
            return "uncertain", "managed child process-start identity cannot be checked"
        if current_identity == child.get("process_start_identity"):
            return "live", "recorded managed child is still live"
        if group_live:
            return "uncertain", "recorded child PID was reused while its process group appears live"
        return "gone", "recorded child PID has been reused by another process"
    if group_live:
        return "uncertain", "managed child exited but its recorded process group remains live"
    return "gone", "recorded managed child and process group are gone"


def _record_child_at_path(path, run_id, child):
    path = Path(path)
    if not path.exists():
        return False
    try:
        with open(str(path), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        raise LockError("ownership record became unreadable while recording managed child", unlockable=False)
    if data.get("run_id") != run_id or data.get("host") != socket.gethostname():
        raise LockError("ownership changed while recording managed child", unlockable=False)
    data["managed_child"] = dict(child)
    data["managed_child_state"] = "launched"
    _replace_json(path, data)
    return True


def _record_launch_state_at_path(path, run_id, state):
    path = Path(path)
    if not path.exists():
        return False
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("run_id") != run_id or data.get("host") != socket.gethostname():
        raise LockError("ownership changed while recording managed child launch state", unlockable=False)
    data["managed_child_state"] = state
    if state != "launched":
        data.pop("managed_child", None)
    _replace_json(path, data)
    return True


def record_managed_child_launch_state(task_dir, repo_root, run_id, state):
    if state not in ("launch_pending", "launch_failed"):
        raise ValueError("invalid managed child launch state")
    task_recorded = _record_launch_state_at_path(lock_path(task_dir), run_id, state)
    worktree_recorded = _record_launch_state_at_path(worktree_lock_path(repo_root), run_id, state)
    if not task_recorded or not worktree_recorded:
        raise LockError("execution ownership disappeared while recording managed child launch state", unlockable=False)


def record_managed_child(task_dir, repo_root, run_id, child):
    """Bind a launched child to both ownership records before it is waited."""
    task_recorded = _record_child_at_path(lock_path(task_dir), run_id, child)
    worktree_recorded = _record_child_at_path(worktree_lock_path(repo_root), run_id, child)
    if not task_recorded or not worktree_recorded:
        raise LockError("execution ownership disappeared before managed child identity was persisted", unlockable=False)


def _holder_description(data):
    return "task=%s run=%s pid=%s host=%s command=%s" % (
        data.get("task") or "unknown",
        data.get("run_id") or "unknown",
        data.get("pid") or "unknown",
        data.get("host") or "unknown",
        data.get("command") or "unknown",
    )


def _write_json_exclusive(path, payload):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(str(path), flags, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    sync_directory(path.parent)


def _replace_json(path, payload):
    atomic_write_json(path, payload, mode=0o644)


class FileOwnershipLock(object):
    def __init__(self, path, command, run_id, task=None, adoption_token=None, kind="lock"):
        self.path = Path(path)
        self.command = command
        self.run_id = run_id
        self.task = task
        self.adoption_token = adoption_token
        self.kind = kind
        self.host = socket.gethostname()
        self.acquired = False

    def _payload(self):
        payload = {
            "pid": os.getpid(),
            "host": self.host,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "command": self.command,
            "run_id": self.run_id,
            "task": self.task,
            "kind": self.kind,
        }
        if self.adoption_token:
            payload["adoption_token"] = self.adoption_token
        return payload

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            if self._adopt_existing():
                return self
            self._raise_existing()
        try:
            _write_json_exclusive(self.path, self._payload())
        except FileExistsError:
            if self._adopt_existing():
                return self
            self._raise_existing()
        self.acquired = True
        return self

    def _read_existing(self):
        try:
            with open(str(self.path), "r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception:
            raise LockError("%s exists but is unreadable; explicit unlock required" % self.kind, unlockable=False)

    def _adopt_existing(self):
        if not self.adoption_token:
            return False
        data = self._read_existing()
        if (
            data.get("host") != self.host
            or data.get("run_id") != self.run_id
            or data.get("adoption_token") != self.adoption_token
        ):
            return False
        data.update({"pid": os.getpid(), "command": self.command, "task": self.task})
        data.pop("adoption_token", None)
        _replace_json(self.path, data)
        self.acquired = True
        return True

    def _raise_existing(self):
        data = self._read_existing()
        holder = _holder_description(data)
        if data.get("host") != self.host:
            raise LockError("%s is held by another host (%s); explicit unlock required" % (self.kind, holder), unlockable=False)
        live = pid_live(data.get("pid"))
        if live is False:
            child_state, child_reason = managed_child_status(data)
            if child_state in ("live", "uncertain"):
                raise LockError("%s controller is gone but %s (%s); refusing recovery" % (self.kind, child_reason, holder), unlockable=False)
            raise LockError("%s holder PID is not live on this host (%s); explicit unlock may be used" % (self.kind, holder), unlockable=True)
        raise LockError("%s is active or liveness is uncertain (%s); explicit unlock required" % (self.kind, holder), unlockable=False)

    def transfer_to(self, pid):
        if not self.path.exists():
            # The child can adopt and finish before the launcher gets CPU
            # again. In that case its successful release is the handoff.
            self.acquired = False
            return
        data = self._read_existing()
        if data.get("run_id") != self.run_id or data.get("host") != self.host:
            raise LockError("cannot transfer %s because its holder changed" % self.kind, unlockable=False)
        data["pid"] = int(pid)
        _replace_json(self.path, data)
        self.acquired = False

    def __exit__(self, exc_type, exc, tb):
        if self.acquired:
            try:
                if not self.path.exists():
                    return
                data = self._read_existing()
                if data.get("pid") == os.getpid() and data.get("run_id") == self.run_id:
                    child_state, unused_reason = managed_child_status(data)
                    if child_state not in ("live", "uncertain"):
                        os.unlink(str(self.path))
            finally:
                self.acquired = False


class TaskLock(FileOwnershipLock):
    def __init__(self, task_dir, command, run_id, task=None, adoption_token=None):
        self.task_dir = task_dir
        FileOwnershipLock.__init__(
            self,
            lock_path(task_dir),
            command,
            run_id,
            task=task or Path(task_dir).name,
            adoption_token=adoption_token,
            kind="task lock",
        )


class WorktreeLock(FileOwnershipLock):
    def __init__(self, repo_root, command, run_id, task, adoption_token=None):
        self.worktree = canonical_git_worktree(repo_root)
        FileOwnershipLock.__init__(
            self,
            self.worktree / ".agent-pipeline" / "execution-lock.json",
            command,
            run_id,
            task=task,
            adoption_token=adoption_token,
            kind="worktree execution ownership",
        )


class ExecutionOwnership(object):
    """Acquire locks in the single supported order: task, then worktree."""
    def __init__(self, task_dir, repo_root, command, run_id, task, adoption_token=None):
        self.task_lock = TaskLock(task_dir, command, run_id, task=task, adoption_token=adoption_token)
        self.worktree_lock = WorktreeLock(repo_root, command, run_id, task=task, adoption_token=adoption_token)

    def __enter__(self):
        self.task_lock.__enter__()
        try:
            self.worktree_lock.__enter__()
        except Exception:
            self.task_lock.__exit__(*sys.exc_info())
            raise
        return self

    def transfer_to(self, pid):
        # Transfer in reverse acquisition order so a partial transfer remains
        # conservatively blocked rather than exposing the worktree.
        self.worktree_lock.transfer_to(pid)
        self.task_lock.transfer_to(pid)

    def __exit__(self, exc_type, exc, tb):
        self.worktree_lock.__exit__(exc_type, exc, tb)
        self.task_lock.__exit__(exc_type, exc, tb)


def _unlockable(path):
    try:
        with open(str(path), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return None, "ownership record is unreadable; refusing to unlock"
    if data.get("host") != socket.gethostname():
        return None, "ownership holder is on another host; refusing to unlock"
    live = pid_live(data.get("pid"))
    if live is True:
        return None, "ownership holder PID is still live; refusing to unlock active ownership"
    if live is None:
        return None, "ownership holder PID liveness is uncertain; refusing to unlock"
    child_state, child_reason = managed_child_status(data)
    if child_state in ("live", "uncertain"):
        return None, "%s; refusing to unlock orphan ownership" % child_reason
    return data, None


def explicit_unlock(task_dir, reason, repo_root=None):
    path = lock_path(task_dir)
    requested_task = Path(task_dir).name
    data = None
    if path.exists():
        data, refusal = _unlockable(path)
        if refusal:
            return {"unlocked": False, "message": refusal}

    execution_path = None
    execution_data = None
    if repo_root is not None:
        execution_path = worktree_lock_path(repo_root)
        if execution_path.exists():
            candidate, execution_refusal = _unlockable(execution_path)
            same_owner = candidate and candidate.get("task") == requested_task
            if data and candidate:
                same_owner = same_owner and candidate.get("run_id") == data.get("run_id")
            if same_owner:
                execution_data = candidate
            elif candidate is None and execution_refusal:
                try:
                    with open(str(execution_path), "r", encoding="utf-8") as handle:
                        current = json.load(handle)
                except Exception:
                    return {"unlocked": False, "message": execution_refusal}
                matches_task = current.get("task") == requested_task
                matches_run = data is not None and current.get("run_id") == data.get("run_id")
                if matches_task or matches_run:
                    return {"unlocked": False, "message": execution_refusal}

    if data is None and execution_data is None:
        return {"unlocked": False, "message": "no lock exists"}

    archive = orchestrator_dir(task_dir) / "runs"
    archive.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    unlocked_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    target = None
    if data is not None:
        target = archive / ("lock-unlocked-%s.json" % stamp)
        data["unlock_reason"] = reason
        data["unlocked_at"] = unlocked_at
        with open(str(target), "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
    if execution_data is not None:
        execution_target = archive / ("worktree-lock-unlocked-%s.json" % stamp)
        execution_data["unlock_reason"] = reason
        execution_data["unlocked_at"] = unlocked_at
        with open(str(execution_target), "w", encoding="utf-8") as handle:
            json.dump(execution_data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.unlink(str(execution_path))
        if target is None:
            target = execution_target
    if data is not None:
        os.unlink(str(path))
    return {"unlocked": True, "message": "lock archived to " + str(target)}
