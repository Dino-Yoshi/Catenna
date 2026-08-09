"""Stage 5 provenance and human-checkpoint helpers."""

from __future__ import print_function

import json
from pathlib import Path

from .artifacts import CONTRACTS, sha256_file, validate_file
from .failures import (
    FAILURE_CLASS_MALFORMED_ARTIFACT,
    FAILURE_CLASS_MAX_TURNS,
    FAILURE_CLASS_STAGE5_AMBIGUITY,
    FAILURE_CLASS_UNKNOWN_FAILURE,
)
from .manifest import validate_manifest
from .overseer import parse_overseer_candidate


def stage5_report_provenance(task_dir, state):
    report_path = task_dir / CONTRACTS["05"].filename
    validation = validate_file(report_path, "05", read_only=True)
    if not validation["valid"]:
        return {"valid": False, "reason": "Stage 5 report is structurally invalid: " + validation["reason"], "failure_class": validation.get("failure_class", FAILURE_CLASS_MALFORMED_ARTIFACT)}
    report_hash = sha256_file(report_path)
    runs = state.get("real_stage_runs", {}).get("05") or []
    for run in reversed(runs):
        if not stage5_run_matches_report(run, report_path, report_hash, state):
            continue
        return {"valid": True, "run": run, "report_hash": report_hash}
    return {"valid": False, "reason": "Stage 5 report exists but no matching successful real Stage 5 provenance record was found", "failure_class": FAILURE_CLASS_STAGE5_AMBIGUITY}


def stage5_run_matches_report(run, report_path, report_hash, state):
    if not isinstance(run, dict):
        return False
    required = ("candidate_artifact_path", "run_id", "pass_number", "attempt_number", "attempt_kind", "retry_reason", "agent", "execution_mode")
    for key in required:
        if run.get(key) in (None, ""):
            return False
    if run.get("execution_mode") != "workspace-write":
        return False
    if run.get("exit_code") not in (0, None):
        return False
    if run.get("failure_class") not in (None, FAILURE_CLASS_MAX_TURNS, FAILURE_CLASS_UNKNOWN_FAILURE):
        return False
    candidate = Path(run.get("candidate_artifact_path"))
    if not candidate.exists() or not candidate.is_file():
        return False
    try:
        candidate_hash = sha256_file(candidate)
    except Exception:
        return False
    if candidate_hash != report_hash:
        return False
    if run.get("final_artifact_hash") and run.get("final_artifact_hash") != report_hash:
        return False
    run["final_artifact_hash"] = report_hash
    run["final_artifact_path"] = str(report_path)
    if not run.get("metadata_path") or not Path(run.get("metadata_path")).exists():
        return False
    if not run.get("stdout_path") or not Path(run.get("stdout_path")).exists():
        return False
    if not run.get("stderr_path") or not Path(run.get("stderr_path")).exists():
        return False
    if not run.get("dirty_baseline") and not state.get("dirty_baseline"):
        return False
    if not run.get("dirty_baseline"):
        run["dirty_baseline"] = state.get("dirty_baseline")
    return True


def stage5_postprocessing_complete(task_dir, state, report_func=stage5_report_provenance):
    report = report_func(task_dir, state)
    if not report["valid"]:
        return report
    manifest_path = task_dir / "05_implementation_manifest.json"
    if not manifest_path.exists():
        return {"valid": False, "reason": "Stage 5 manifest is missing", "failure_class": FAILURE_CLASS_STAGE5_AMBIGUITY}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        validate_manifest(manifest)
    except Exception as exc:
        return {"valid": False, "reason": "Stage 5 manifest is invalid: " + str(exc), "failure_class": FAILURE_CLASS_STAGE5_AMBIGUITY}
    manifest_run = manifest.get("stage5_run") or {}
    if manifest.get("stage") != "05":
        return {"valid": False, "reason": "Stage 5 manifest has wrong stage", "failure_class": FAILURE_CLASS_STAGE5_AMBIGUITY}
    if manifest_run.get("run_id") != report["run"].get("run_id"):
        return {"valid": False, "reason": "Stage 5 manifest run id does not match current Stage 5 artifact", "failure_class": FAILURE_CLASS_STAGE5_AMBIGUITY}
    if manifest_run.get("candidate_artifact_path") != report["run"].get("candidate_artifact_path"):
        return {"valid": False, "reason": "Stage 5 manifest candidate path does not match current Stage 5 artifact", "failure_class": FAILURE_CLASS_STAGE5_AMBIGUITY}
    if manifest_run.get("final_artifact_hash") != report["report_hash"]:
        return {"valid": False, "reason": "Stage 5 manifest artifact hash does not match current Stage 5 report", "failure_class": FAILURE_CLASS_STAGE5_AMBIGUITY}
    overseer = state.get("overseer") or {}
    required_paths = {
        "json_path": task_dir / "05_supervisor_handoff.json",
        "markdown_path": task_dir / "05_supervisor_handoff.md",
        "legacy_path": task_dir / "handoff.md",
    }
    for key, expected in required_paths.items():
        recorded = overseer.get(key)
        if not recorded:
            return {"valid": False, "reason": "Stage 5 handoff state is missing " + key, "failure_class": FAILURE_CLASS_STAGE5_AMBIGUITY}
        if Path(recorded) != expected or not expected.exists():
            return {"valid": False, "reason": "Stage 5 handoff path is missing or inconsistent: " + key, "failure_class": FAILURE_CLASS_STAGE5_AMBIGUITY}
    try:
        parse_overseer_candidate(json.loads(required_paths["json_path"].read_text(encoding="utf-8")))
    except Exception as exc:
        return {"valid": False, "reason": "Stage 5 supervisor handoff JSON is invalid: " + str(exc), "failure_class": FAILURE_CLASS_STAGE5_AMBIGUITY}
    if state.get("state") != "awaiting_human_test":
        return {"valid": False, "reason": "State is not awaiting human Stage 6 testing", "failure_class": FAILURE_CLASS_STAGE5_AMBIGUITY}
    if state.get("current_stage") != "06":
        return {"valid": False, "reason": "Current stage is not 06", "failure_class": FAILURE_CLASS_STAGE5_AMBIGUITY}
    checkpoint = state.get("human_checkpoint")
    if not isinstance(checkpoint, dict) or checkpoint.get("stage") != "06":
        return {"valid": False, "reason": "Human checkpoint for Stage 6 is missing", "failure_class": FAILURE_CLASS_STAGE5_AMBIGUITY}
    return {"valid": True, "run": report["run"], "manifest": manifest}


def any_stage5_postprocessing_present(task_dir, state):
    if state.get("manifest") or state.get("overseer") or state.get("human_checkpoint"):
        return True
    for name in ("05_implementation_manifest.json", "05_supervisor_handoff.json", "05_supervisor_handoff.md", "handoff.md"):
        if (task_dir / name).exists():
            return True
    return False


def checkpoint_noop_eligible(task_dir, state, postprocessing_func=stage5_postprocessing_complete):
    if state.get("state") != "awaiting_human_test" or state.get("current_stage") != "06":
        return {"eligible": False, "reason": "not at human checkpoint"}
    post = postprocessing_func(task_dir, state)
    if not post["valid"]:
        return {"eligible": False, "reason": post["reason"]}
    stage06_validation = validate_file(task_dir / CONTRACTS["06"].filename, "06", read_only=True)
    if stage06_validation["valid"]:
        return {"eligible": False, "reason": "Stage 6 manual test notes are ready; resuming to drive Stage 7/8"}
    checkpoint = state.get("human_checkpoint") or {}
    recorded = checkpoint.get("noop_hashes")
    if not isinstance(recorded, dict):
        return {"eligible": False, "reason": "checkpoint hash set is missing"}
    current = checkpoint_hashes(task_dir, state)
    if current != recorded:
        return {"eligible": False, "reason": "checkpoint hash set changed"}
    return {"eligible": True, "reason": "unchanged human checkpoint"}


def checkpoint_hashes(task_dir, state):
    paths = []
    for key in ("00", "01", "02", "03", "04", "04_gate", "05"):
        paths.append(task_dir / CONTRACTS[key].filename)
    for name in ("05_implementation_manifest.json", "05_supervisor_handoff.json", "05_supervisor_handoff.md", "handoff.md"):
        paths.append(task_dir / name)
    stage5 = _last_stage_result(state, "05")
    if stage5 and stage5.get("candidate_artifact_path"):
        candidate = Path(stage5["candidate_artifact_path"])
        final_report = task_dir / CONTRACTS["05"].filename
        try:
            if candidate.resolve() != final_report.resolve():
                paths.append(candidate)
        except Exception:
            paths.append(candidate)
    result = {}
    for path in paths:
        label = str(path)
        if not path.exists() or not path.is_file():
            result[label] = None
        else:
            result[label] = sha256_file(path)
    return result


def _last_stage_result(state, stage_key):
    runs = state.get("real_stage_runs", {}).get(stage_key) or []
    return runs[-1] if runs else None
