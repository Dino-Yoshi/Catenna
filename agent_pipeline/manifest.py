"""Stage 5 implementation manifest helpers."""

from __future__ import print_function

import json
import subprocess
import time
from pathlib import Path

from .artifacts import sha256_file


VERIFICATION_STATUSES = set(["passed", "failed", "blocked", "not_attempted"])


def capture_dirty_baseline(repo_root):
    entries = git_status(repo_root)
    hashes = {}
    for entry in entries:
        path = entry_path(entry)
        full = repo_root / path
        if full.exists() and full.is_file():
            hashes[path] = sha256_file(full)
    return {"captured_at": now(), "entries": entries, "hashes": hashes}


def changed_files_since(repo_root, baseline):
    before_paths = set(entry_path(entry) for entry in baseline.get("entries", []))
    before_hashes = baseline.get("hashes", {})
    after_entries = git_status(repo_root)
    after_paths = set(entry_path(entry) for entry in after_entries)
    changed = []
    for path in sorted(after_paths):
        full = repo_root / path
        before_present = path in before_paths
        after_hash = sha256_file(full) if full.exists() and full.is_file() else None
        if not before_present:
            changed.append({"path": path, "reason": "absent_before_present_after"})
        elif path not in before_hashes:
            changed.append({"path": path, "reason": "status_changed_after_stage5"})
        elif before_hashes.get(path) != after_hash:
            changed.append({"path": path, "reason": "pre_dirty_hash_changed_during_stage5"})
    return changed


def write_manifest(task_dir, repo_root, state, stage5_result, baseline):
    changed = changed_files_since(repo_root, baseline)
    if isinstance(stage5_result, dict):
        stage5_result["dirty_changed_files"] = changed
    manifest = {
        "schema_version": 1,
        "generated_at": now(),
        "task": state.get("task"),
        "stage": "05",
        "stage5_run": stage5_result,
        "changed_files": changed,
        "verification": {
            "unit_tests": "not_attempted",
            "mock_pipeline": "not_attempted",
            "diff_check": "not_attempted",
        },
        "verification_evidence": [],
    }
    validate_manifest(manifest)
    path = task_dir / "05_implementation_manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    state["manifest"] = {"path": str(path), "status": "generated", "generated_at": manifest["generated_at"]}
    return manifest


def validate_manifest(manifest):
    for key, value in manifest.get("verification", {}).items():
        if value not in VERIFICATION_STATUSES:
            raise ValueError("invalid verification status for %s: %s" % (key, value))
    if not isinstance(manifest.get("changed_files"), list):
        raise ValueError("manifest changed_files must be a list")


def git_status(repo_root):
    output = subprocess.check_output(
        [
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            ".",
            ":(exclude).agent-pipeline",
        ],
        cwd=str(repo_root),
    )
    return [line for line in output.decode("utf-8", "replace").splitlines() if line.strip()]


def entry_path(entry):
    path = entry[3:] if len(entry) > 3 else entry
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    return path.strip()


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
