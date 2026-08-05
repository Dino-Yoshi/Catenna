"""Atomic artifact write helpers for the mock controller."""

from __future__ import print_function

import json
import os
import time

from .artifacts import CONTRACTS, validate_file
from .state import orchestrator_dir


def ensure_dirs(task_dir):
    root = orchestrator_dir(task_dir)
    for name in ("failed", "runs", "traces"):
        (root / name).mkdir(parents=True, exist_ok=True)


def preserve_failed(task_dir, stage_key, output, reason, metadata=None):
    ensure_dirs(task_dir)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    base = "%s-%s" % (stage_key, stamp)
    failed_dir = orchestrator_dir(task_dir) / "failed"
    output_path = failed_dir / (base + ".out")
    meta_path = failed_dir / (base + ".json")
    with open(str(output_path), "w", encoding="utf-8") as handle:
        handle.write(output)
    payload = {"stage": stage_key, "reason": reason}
    if metadata:
        payload.update(metadata)
    with open(str(meta_path), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return str(output_path)


def atomic_finalize(task_dir, stage_key, output, read_only=False):
    ensure_dirs(task_dir)
    contract = CONTRACTS[stage_key]
    destination = task_dir / contract.filename
    task_dir.mkdir(parents=True, exist_ok=True)
    temp_path = task_dir / (contract.filename + ".candidate.%d" % os.getpid())
    with open(str(temp_path), "w", encoding="utf-8") as handle:
        handle.write(output)
    validation = validate_file(temp_path, stage_key, read_only=read_only)
    if not validation["valid"]:
        failed = preserve_failed(
            task_dir,
            stage_key,
            output,
            validation["reason"],
            {"failure_class": validation.get("failure_class")},
        )
        try:
            os.unlink(str(temp_path))
        except OSError:
            pass
        return {"finalized": False, "validation": validation, "failed_path": failed}
    os.replace(str(temp_path), str(destination))
    return {"finalized": True, "validation": validation, "path": str(destination)}
