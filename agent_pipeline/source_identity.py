"""Source identity: a content fingerprint of the driven project's worktree.

Covers the HEAD commit, the index (staged blob ids and modes), tracked
working-file contents/executable bits/deletions, and non-ignored untracked
files. Symlinks are represented by their link target text and are never
followed; a path whose parent component is a symlink is recorded as such
rather than read through the link. Controller-owned `.agent-pipeline/` data
and any other caller-supplied controller paths are excluded. Ignored files
(build output) are outside the identity by construction.

Any failure to produce a complete, self-consistent snapshot raises
SourceIdentityError; callers treat that as "unverified", never as a match.
"""

from __future__ import print_function

import errno
import hashlib
import json
import os
import re
import stat
import subprocess
import time
from pathlib import Path


IDENTITY_SCHEMA = 1
IDENTITY_PREFIX = "sha256:"
# Always excluded, relative to any directory level: controller-owned data.
ALWAYS_EXCLUDED_GLOBS = ("**/.agent-pipeline", "**/.agent-pipeline/**")
# Created by the controller's own Gradle verification check at the repo root.
CONTROLLER_CREATED_DIRS = (".gradle-user-home",)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_COMPONENTS = ("head", "index", "worktree", "untracked")


# R07-FR1: a capture whose hashed content keeps changing underneath it is
# retried, then rejected rather than accepted as a snapshot.
CAPTURE_ATTEMPTS = 3
# Timestamps can be coarser than a write, so a same-size rewrite may leave
# lstat unchanged ("racy git"). Entries this recent are re-hashed at the end.
RACY_WINDOW_NS = 2 * 10 ** 9
# R07-FR2: at most this many changed paths are named in a stale reason.
MAX_REPORTED_PATHS = 20
# R07-FR3 (operator decision 3): identity stays strict; explain how to fix it.
UNTRACKED_OUTPUT_HINT = (
    "untracked files changed; output created by a check must be ignored by Git "
    "(for example, `__pycache__/`), or verification will never be current"
)


class SourceIdentityError(Exception):
    pass


class _ContentChanged(SourceIdentityError):
    """A hashed entry changed while the snapshot was being captured."""


def capture_source_identity(repo_root, exclude_paths=None):
    """Return a complete source identity for repo_root's Git worktree."""
    repo_root = Path(repo_root)
    top = _toplevel(repo_root)
    pathspec = _pathspec(top, repo_root, exclude_paths)
    for _ in range(CAPTURE_ATTEMPTS):
        try:
            return _capture_once(top, pathspec)
        except _ContentChanged as exc:
            last = exc
    raise SourceIdentityError(
        "source content kept changing while its identity was being captured (%d attempts): %s"
        % (CAPTURE_ATTEMPTS, last)
    )


def _capture_once(top, pathspec):
    started_ns = time.time_ns()
    head_before = _head(top)
    index_before = _git(top, ["ls-files", "--stage", "-z", "--"] + pathspec)
    tracked = _paths(_git(top, ["ls-files", "-z", "--"] + pathspec))
    untracked = _paths(_git(top, ["ls-files", "--others", "--exclude-standard", "-z", "--"] + pathspec))
    worktree = _hash_entries(top, tracked, allow_missing=True)
    untracked_entries = _hash_entries(top, untracked, allow_missing=False)
    # A listing that moved underneath the content hashing is not a snapshot.
    if _head(top) != head_before or _git(top, ["ls-files", "--stage", "-z", "--"] + pathspec) != index_before:
        raise SourceIdentityError("source changed while its identity was being captured")
    # R07-FR1: every hashed entry must still be exactly what was read, so the
    # identity describes one state of the tree rather than a mix of two.
    for entries, allow_missing in ((worktree, True), (untracked_entries, False)):
        for rel, entry in entries:
            signature = entries.signatures.get(rel)
            if _signature(top, rel) != signature:
                raise _ContentChanged("%s changed after it was hashed" % rel)
            if _is_racy(top, rel, started_ns) and _entry(top, rel, allow_missing) != entry:
                raise _ContentChanged("%s changed within its timestamp resolution" % rel)
    identity = {
        "schema": IDENTITY_SCHEMA,
        "head": head_before,
        "index": _digest(index_before),
        "worktree": _digest(_canonical(worktree)),
        "untracked": _digest(_canonical(untracked_entries)),
        "tracked_count": len(tracked),
        "untracked_count": len(untracked),
        # Diagnostic detail is deliberately outside compute_fingerprint(): the
        # four authoritative components above remain the stable C06 identity,
        # while C07 can still report which persistent paths changed.
        "path_fingerprints": _path_fingerprints(index_before, worktree, untracked_entries),
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    identity["fingerprint"] = compute_fingerprint(identity)
    return identity


def compute_fingerprint(identity):
    payload = {"schema": identity.get("schema")}
    for key in _COMPONENTS:
        payload[key] = identity.get(key)
    return _digest(_canonical(payload))


def identity_problem(identity):
    """Return None for a complete, internally consistent identity record,
    else a reason it cannot be trusted."""
    if not isinstance(identity, dict):
        return "source identity is missing"
    if identity.get("schema") != IDENTITY_SCHEMA:
        return "source identity has an unsupported schema"
    for key in ("index", "worktree", "untracked", "fingerprint"):
        if not isinstance(identity.get(key), str) or not _HEX64.match(identity[key]):
            return "source identity is incomplete: %s" % key
    head = identity.get("head")
    if head is not None and (not isinstance(head, str) or not re.match(r"^[0-9a-f]{40}([0-9a-f]{24})?$", head)):
        return "source identity has an invalid head"
    if compute_fingerprint(identity) != identity["fingerprint"]:
        return "source identity is internally inconsistent"
    return None


def same_identity(left, right):
    return (
        identity_problem(left) is None
        and identity_problem(right) is None
        and left["fingerprint"] == right["fingerprint"]
    )


def changed_identity_paths(left, right):
    """Return paths known to differ between two complete identities."""
    if identity_problem(left) is not None or identity_problem(right) is not None:
        return []
    before = left.get("path_fingerprints") or {}
    after = right.get("path_fingerprints") or {}
    changed = sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))
    if left.get("head") != right.get("head"):
        changed.append("<HEAD>")
    if left.get("index") != right.get("index") and not any(key.startswith("index:") for key in changed):
        changed.append("<index>")
    return changed


def describe_identity_change(left, right):
    """R07-FR2/FR3: the changed paths between two complete identities as a
    reason suffix, or "" when they are unknown."""
    paths = changed_identity_paths(left, right)
    if not paths:
        return ""
    text = "changed paths: " + ", ".join(paths[:MAX_REPORTED_PATHS])
    if len(paths) > MAX_REPORTED_PATHS:
        text += " (and %d more)" % (len(paths) - MAX_REPORTED_PATHS)
    if any(path.startswith("untracked:") for path in paths):
        text += "; " + UNTRACKED_OUTPUT_HINT
    return text


def with_identity_change(reason, left, right):
    detail = describe_identity_change(left, right)
    return "%s (%s)" % (reason, detail) if detail else reason


def _path_fingerprints(index_raw, worktree, untracked):
    result = {}
    index_entries = {}
    for record in index_raw.split(b"\0"):
        if not record or b"\t" not in record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        path = raw_path.decode("utf-8", "surrogateescape")
        index_entries.setdefault(path, []).append(metadata.decode("ascii", "replace"))
    for path, values in index_entries.items():
        result["index:" + path] = _digest(_canonical(sorted(values)))
    for path, entry in worktree:
        result["worktree:" + path] = _digest(_canonical(entry))
    for path, entry in untracked:
        result["untracked:" + path] = _digest(_canonical(entry))
    return result


def format_identity(identity):
    return IDENTITY_PREFIX + identity["fingerprint"]


def parse_cited_identity(text):
    """Return the identity fingerprints cited in a `## Source identity`
    section: (fingerprints, section_count)."""
    sections = []
    current = None
    for line in (text or "").splitlines():
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            current = [] if match.group(1).strip().lower() == "source identity" else None
            if current is not None:
                sections.append(current)
            continue
        if current is not None:
            current.append(line)
    cited = []
    for body in sections:
        for token in re.findall(r"(?:sha256:)?([0-9a-fA-F]{64})(?![0-9a-fA-F])", "\n".join(body)):
            value = token.lower()
            if value not in cited:
                cited.append(value)
    return cited, len(sections)


def _toplevel(repo_root):
    try:
        out = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], cwd=str(repo_root), stderr=subprocess.PIPE)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SourceIdentityError("not a readable Git worktree: %s" % _describe(exc))
    return Path(out.decode("utf-8", "surrogateescape").strip()).resolve()


def _head(top):
    try:
        out = subprocess.check_output(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=str(top), stderr=subprocess.PIPE)
        return out.decode("ascii").strip()
    except subprocess.CalledProcessError as exc:
        # rev-parse -q exits 1 silently only for a missing ref (unborn HEAD).
        if exc.returncode == 1 and not (exc.stderr or b"").strip():
            return None
        raise SourceIdentityError("cannot read HEAD: %s" % _describe(exc))
    except OSError as exc:
        raise SourceIdentityError("cannot read HEAD: %s" % exc)


def _git(top, args):
    try:
        return subprocess.check_output(["git", "-c", "core.quotePath=false"] + args, cwd=str(top), stderr=subprocess.PIPE)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SourceIdentityError("git %s failed: %s" % (args[0], _describe(exc)))


def _describe(exc):
    stderr = getattr(exc, "stderr", None)
    if stderr:
        return stderr.decode("utf-8", "replace").strip()
    return str(exc)


def _pathspec(top, repo_root, exclude_paths):
    spec = [":(top)"]
    spec.extend(":(top,exclude,glob)" + pattern for pattern in ALWAYS_EXCLUDED_GLOBS)
    candidates = [Path(repo_root) / name for name in CONTROLLER_CREATED_DIRS]
    candidates.extend(Path(path) for path in (exclude_paths or []))
    for path in candidates:
        try:
            rel = Path(os.path.abspath(str(path))).resolve().relative_to(top)
        except (ValueError, OSError):
            continue
        rel_text = rel.as_posix()
        if rel_text in ("", "."):
            # Never exclude the whole worktree.
            continue
        spec.append(":(top,exclude,literal)" + rel_text)
    return spec


def _paths(raw):
    seen = []
    known = set()
    for item in raw.split(b"\0"):
        if not item:
            continue
        path = item.decode("utf-8", "surrogateescape")
        if path not in known:
            known.add(path)
            seen.append(path)
    return sorted(seen)


class _Entries(list):
    """Hashed [path, entry] pairs plus the lstat signature each was read at."""

    def __init__(self):
        list.__init__(self)
        self.signatures = {}


def _hash_entries(top, paths, allow_missing):
    entries = _Entries()
    for rel in paths:
        before = _signature(top, rel)
        entry = _entry(top, rel, allow_missing)
        # R07-FR1: an entry whose metadata moved while it was read is torn.
        if _signature(top, rel) != before:
            raise _ContentChanged("%s changed while it was being hashed" % rel)
        entries.signatures[rel] = before
        entries.append([rel, entry])
    return entries


def _stat_key(info):
    if stat.S_ISDIR(info.st_mode):
        # Only the kind of a directory entry is part of the identity.
        return ("directory", info.st_dev, info.st_ino)
    return (
        stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), info.st_dev, info.st_ino,
        info.st_size, info.st_mtime_ns, info.st_ctime_ns,
    )


def _is_racy(top, rel, started_ns):
    if _signature(top, rel)[0] != "entry":
        return False
    try:
        info = os.lstat(str(top / rel))
    except OSError:
        # Vanished since the signature check; the re-hash reports it.
        return True
    if not stat.S_ISREG(info.st_mode):
        return False
    return max(info.st_mtime_ns, info.st_ctime_ns) >= started_ns - RACY_WINDOW_NS


def _signature(top, rel):
    """The lstat metadata that determines `_entry(top, rel)`, walking the
    same components without following symlinks. Errors are part of the
    signature so that `_entry` reports them itself."""
    current = top
    parts = rel.split("/")
    for part in parts[:-1]:
        current = current / part
        try:
            info = os.lstat(str(current))
        except OSError as exc:
            return ("error", exc.errno)
        if stat.S_ISLNK(info.st_mode):
            return ("beyond_symlink", _stat_key(info))
    try:
        return ("entry", _stat_key(os.lstat(str(top / rel))))
    except OSError as exc:
        return ("error", exc.errno)


def _entry(top, rel, allow_missing):
    parts = rel.split("/")
    current = top
    for part in parts[:-1]:
        current = current / part
        try:
            info = os.lstat(str(current))
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ENOTDIR) and allow_missing:
                return {"kind": "missing"}
            raise SourceIdentityError("cannot inspect %s: %s" % (rel, exc))
        if stat.S_ISLNK(info.st_mode):
            # Never read through a symlinked directory; it may leave the worktree.
            return {"kind": "beyond_symlink"}
    full = top / rel
    try:
        info = os.lstat(str(full))
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR) and allow_missing:
            return {"kind": "missing"}
        raise SourceIdentityError("cannot inspect %s: %s" % (rel, exc))
    if stat.S_ISLNK(info.st_mode):
        try:
            return {"kind": "symlink", "target": os.readlink(str(full))}
        except OSError as exc:
            raise SourceIdentityError("cannot read symlink %s: %s" % (rel, exc))
    if stat.S_ISDIR(info.st_mode):
        # A gitlink/submodule or a directory replacing a tracked file.
        return {"kind": "directory"}
    if not stat.S_ISREG(info.st_mode):
        return {"kind": "special", "mode": stat.S_IFMT(info.st_mode)}
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(full), flags)
    except OSError as exc:
        raise SourceIdentityError("cannot read %s: %s" % (rel, exc))
    try:
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if _stat_key(opened) != _stat_key(info):
                raise _ContentChanged("%s was replaced while it was being opened" % rel)
            digest = _hash_stream(handle)
            if _stat_key(os.fstat(handle.fileno())) != _stat_key(info):
                raise _ContentChanged("%s changed while it was being hashed" % rel)
    except OSError as exc:
        raise SourceIdentityError("cannot read %s: %s" % (rel, exc))
    return {"kind": "file", "executable": bool(info.st_mode & 0o111), "sha256": digest}


def _hash_stream(handle):
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(65536), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8", "surrogateescape")


def _digest(data):
    return hashlib.sha256(data).hexdigest()
