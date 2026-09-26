"""Crash-safe local-file persistence used by controller transitions."""

from __future__ import print_function

import json
import os
import uuid
from pathlib import Path


class DurableStorageError(Exception):
    pass


def sync_directory(path):
    """Persist directory-entry changes on supported local POSIX filesystems."""
    fd = None
    try:
        fd = os.open(str(Path(path)), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        os.fsync(fd)
    except Exception as exc:
        raise DurableStorageError("cannot synchronize directory %s: %s" % (path, exc))
    finally:
        if fd is not None:
            os.close(fd)


def ensure_directory(path):
    path = Path(path)
    try:
        path.mkdir(parents=True, exist_ok=True)
        sync_directory(path)
    except DurableStorageError:
        raise
    except Exception as exc:
        raise DurableStorageError("cannot create durable directory %s: %s" % (path, exc))
    return path


def atomic_write_bytes(path, payload, mode=0o600):
    path = Path(path)
    ensure_directory(path.parent)
    temporary = path.parent / (path.name + ".tmp.%d.%s" % (os.getpid(), uuid.uuid4().hex))
    fd = None
    try:
        fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
        sync_directory(path.parent)
    except DurableStorageError:
        raise
    except Exception as exc:
        raise DurableStorageError("cannot durably replace %s: %s" % (path, exc))
    finally:
        if fd is not None:
            os.close(fd)
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


def atomic_write_text(path, text, mode=0o600):
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def atomic_write_json(path, data, mode=0o600):
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n", mode=mode)


def sync_file(path):
    path = Path(path)
    fd = None
    try:
        fd = os.open(str(path), os.O_RDONLY)
        os.fsync(fd)
        sync_directory(path.parent)
    except DurableStorageError:
        raise
    except Exception as exc:
        raise DurableStorageError("cannot synchronize file %s: %s" % (path, exc))
    finally:
        if fd is not None:
            os.close(fd)


def write_json_exclusive(path, data, mode=0o600):
    """Create an immutable transition record; never replace an existing one."""
    path = Path(path)
    ensure_directory(path.parent)
    fd = None
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        sync_directory(path.parent)
    except Exception as exc:
        if isinstance(exc, DurableStorageError):
            raise
        raise DurableStorageError("cannot create durable record %s: %s" % (path, exc))
    finally:
        if fd is not None:
            os.close(fd)
