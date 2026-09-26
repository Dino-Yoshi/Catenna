"""Content postconditions for controller-owned inputs and records."""

from __future__ import print_function

import hashlib
import os
import stat
from pathlib import Path


class IntegrityError(Exception):
    pass


def capture_paths(roots, excluded_paths=()):
    """Fingerprint every persistent entry below roots without following links."""
    excluded = {_absolute(path) for path in excluded_paths}
    entries = {}
    for root in roots:
        root = Path(root)
        absolute_root = _absolute(root)
        if absolute_root in excluded:
            continue
        if not root.exists() and not root.is_symlink():
            continue
        if root.is_file() or root.is_symlink():
            entries[absolute_root] = _entry(root)
            continue
        try:
            for directory, dirnames, filenames in os.walk(str(root), topdown=True, followlinks=False):
                directory_path = Path(directory)
                kept = []
                for name in sorted(dirnames):
                    path = directory_path / name
                    absolute = _absolute(path)
                    if absolute in excluded:
                        continue
                    if path.is_symlink():
                        entries[absolute] = _entry(path)
                    else:
                        kept.append(name)
                dirnames[:] = kept
                for name in sorted(filenames):
                    path = directory_path / name
                    absolute = _absolute(path)
                    if absolute not in excluded:
                        entries[absolute] = _entry(path)
        except OSError as exc:
            raise IntegrityError("cannot inspect protected records under %s: %s" % (root, exc))
    return entries


def changed_paths(before, after):
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))


def _absolute(path):
    return os.path.abspath(str(path))


def _entry(path):
    try:
        info = os.lstat(str(path))
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            payload = os.readlink(str(path)).encode("utf-8", "surrogateescape")
            kind = "symlink"
        elif stat.S_ISREG(info.st_mode):
            digest = hashlib.sha256()
            with open(str(path), "rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            payload = digest.hexdigest().encode("ascii")
            kind = "file"
        else:
            raise IntegrityError("unsupported protected record type: %s" % path)
    except (OSError, IntegrityError) as exc:
        if isinstance(exc, IntegrityError):
            raise
        raise IntegrityError("cannot inspect protected record %s: %s" % (path, exc))
    return "%s:%o:%s" % (kind, mode, payload.decode("ascii", "surrogateescape"))
