"""Durable per-attempt dispatch, completion, promotion, and recovery records."""

from __future__ import print_function

import hashlib
import json
import uuid
from pathlib import Path

from .artifacts import CONTRACTS, sha256_file, validate_text
from .durable import atomic_write_text, sync_file, write_json_exclusive
from .state import (
    STAGE_CONSUMED_INPUTS,
    acknowledge_recorded_consumed_inputs,
    orchestrator_dir,
    write_state_atomic,
)


# F02: agent stages whose attempt budget is granted once per consumed-input
# identity.  Stage 7 keeps its R02 review-input identity instead.
BUDGETED_STAGES = ("02", "03", "04", "04_gate", "05")


def attempts_root(task_dir):
    return orchestrator_dir(task_dir) / "attempts"


def stage_input_identity(stage, consumed_input_hashes):
    """F02-FR1: digest of the stage key and its dispatch-time consumed inputs.

    Returns None for stages outside BUDGETED_STAGES and for records without
    consumed-input provenance (F02-FR5).
    """
    if stage not in BUDGETED_STAGES or not isinstance(consumed_input_hashes, dict):
        return None
    payload = {"stage": stage, "consumed_input_hashes": consumed_input_hashes}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def current_stage_input_identity(task_dir, stage):
    """The identity a dispatch of `stage` would record now (missing inputs hash as null)."""
    if stage not in BUDGETED_STAGES:
        return None
    hashes = {}
    for consumed_stage in STAGE_CONSUMED_INPUTS.get(stage, []):
        path = Path(task_dir) / CONTRACTS[consumed_stage].filename
        hashes[path.name] = sha256_file(path) if path.is_file() else None
    return stage_input_identity(stage, hashes)


def prepare_dispatch(task_dir, state, stage, agent, mode, pass_number, attempt_number,
                     attempt_kind, retry_reason, prompt_path, input_hashes, baseline,
                     effective_config, review_input_identity=None):
    if stage == "07" and (not isinstance(review_input_identity, str) or not review_input_identity):
        raise ValueError("Stage 7 dispatch requires a review-input identity")
    attempt_id = uuid.uuid4().hex
    root = attempts_root(task_dir) / attempt_id
    prompt_snapshot = root / "prompt.md"
    prompt_text = Path(prompt_path).read_text(encoding="utf-8")
    atomic_write_text(prompt_snapshot, prompt_text)
    # Capture the artifacts the attempt is actually about.  The acknowledged
    # hashes in state may intentionally describe an older accepted chain and
    # therefore cannot establish dispatch-time provenance.
    consumed_input_hashes = {}
    for consumed_stage in STAGE_CONSUMED_INPUTS.get(stage, []):
        contract = CONTRACTS[consumed_stage]
        consumed_input_hashes[contract.filename] = sha256_file(Path(task_dir) / contract.filename)
    dispatch = {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "task": state.get("task"),
        "run_id": state.get("run_id"),
        "stage": stage,
        "agent": agent,
        "execution_mode": mode,
        "pass_number": pass_number,
        "attempt_number": attempt_number,
        "attempt_kind": attempt_kind,
        "retry_reason": retry_reason,
        "prompt_path": str(prompt_snapshot),
        "prompt_hash": sha256_file(prompt_snapshot),
        "input_hashes": dict(consumed_input_hashes),
        "consumed_input_hashes": dict(consumed_input_hashes),
        "implementation_baseline": baseline,
        "attempts_consumed": dict(state.get("attempts") or {}),
        "approval": dict(state.get("pending_approval") or {}),
        "effective_agent_config": effective_config,
    }
    if stage == "07":
        dispatch["review_input_identity"] = review_input_identity
    if stage in BUDGETED_STAGES:
        dispatch["stage_input_identity"] = stage_input_identity(stage, consumed_input_hashes)
    write_json_exclusive(root / "dispatch.json", dispatch)
    state.setdefault("durable_attempts", {})[attempt_id] = {
        "stage": stage, "attempt_number": attempt_number, "status": "dispatched"
    }
    # This is deliberately the last operation before the provider call.
    write_state_atomic(task_dir, state)
    return dispatch


def count_stage7_attempts(task_dir, review_input_identity):
    """Reconstruct the consumed R02 allowance from immutable dispatches."""
    if not isinstance(review_input_identity, str) or not review_input_identity:
        return 0
    root = attempts_root(task_dir)
    if not root.exists():
        return 0
    used = 0
    for attempt_dir in root.iterdir():
        if not attempt_dir.is_dir():
            continue
        dispatch_path = attempt_dir / "dispatch.json"
        if not dispatch_path.is_file():
            continue
        dispatch = _load(dispatch_path)
        if (dispatch.get("stage") == "07" and
                dispatch.get("review_input_identity") == review_input_identity):
            used += 1
    return used


def count_stage_input_attempts(task_dir, stage, identity):
    """F02-FR2: attempts used by one stage input identity, from immutable dispatches.

    Every durable dispatch counts, including one whose controller crashed
    before completion.  The identity is derived from each record's
    `consumed_input_hashes`, so a dispatch without that provenance counts
    toward no identity (F02-FR5).  Max-turn completion retries are excluded:
    the live loop never charges them against the stage budget either.
    """
    if not isinstance(identity, str) or not identity:
        return 0
    root = attempts_root(task_dir)
    if not root.exists():
        return 0
    used = 0
    for attempt_dir in root.iterdir():
        dispatch_path = attempt_dir / "dispatch.json"
        if not attempt_dir.is_dir() or not dispatch_path.is_file():
            continue
        dispatch = _load(dispatch_path)
        if (dispatch.get("stage") != stage or
                dispatch.get("attempt_kind") == "completion_only_retry"):
            continue
        if stage_input_identity(stage, dispatch.get("consumed_input_hashes")) == identity:
            used += 1
    return used


def record_completion(task_dir, result, normalized_output, validation, process_succeeded):
    attempt_id = result.get("attempt_id")
    if not attempt_id:
        return None
    root = attempts_root(task_dir) / attempt_id
    # The immutable completion record must not point at output which is only
    # present in the page cache. Missing output is likewise a storage failure,
    # never a successful completion.
    for key in ("candidate_artifact_path", "metadata_path", "stdout_path", "stderr_path"):
        path = result.get(key)
        if path:
            sync_file(path)
    result_path = root / "result.md"
    atomic_write_text(result_path, normalized_output)
    public_result = {key: value for key, value in result.items() if not key.startswith("_")}
    record = {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "stage": result.get("stage"),
        "status": result.get("status"),
        "result_path": str(result_path),
        "result_hash": sha256_file(result_path),
        "candidate_path": result.get("candidate_artifact_path"),
        "candidate_hash": _optional_hash(result.get("candidate_artifact_path")),
        "metadata_path": result.get("metadata_path"),
        "process_succeeded": bool(process_succeeded),
        "artifact_valid": bool(validation.get("valid")),
        "postcondition_valid": bool((result.get("postcondition") or {}).get("valid")),
        "eligible_for_promotion": bool(process_succeeded and validation.get("valid") and (result.get("postcondition") or {}).get("valid")),
        "result": public_result,
    }
    write_json_exclusive(root / "completed.json", record)
    return record


def record_promotion(task_dir, result, final_path):
    attempt_id = result.get("attempt_id")
    if not attempt_id:
        return
    record = {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "stage": result.get("stage"),
        "final_path": str(final_path),
        "final_hash": sha256_file(final_path),
    }
    write_json_exclusive(attempts_root(task_dir) / attempt_id / "promoted.json", record)


def record_writer_resolution(task_dir, attempt_id, stage, resolution):
    """Durably record that a failed writer was safely compared/resolved."""
    record = {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "stage": stage,
        "resolution": resolution,
    }
    write_json_exclusive(attempts_root(task_dir) / attempt_id / "writer-resolution.json", record)


def invalidate_promoted(task_dir, stage_keys, reason, history_path):
    """Durably retire promotions whose canonical artifacts were archived.

    This must run after the history copy exists and before the canonical files
    are removed.  The sidecar is immutable so recovery cannot later mistake a
    deliberately archived result for an interrupted promotion.

    F04: every un-retired promotion of an invalidated stage is retired, whether
    the canonical file matched, differed from (e.g. a hand edit), or was
    missing relative to the promotion.  Otherwise recovery would re-promote the
    original result over the operator's change.  `history_path` is None when
    nothing was archived.
    """
    wanted = set(stage_keys)
    root = attempts_root(task_dir)
    if not root.exists():
        return []
    history_path = Path(history_path) if history_path is not None else None
    invalidated = []
    for attempt_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        promoted_path = attempt_dir / "promoted.json"
        if not promoted_path.exists():
            continue
        promoted = _load(promoted_path)
        stage = promoted.get("stage")
        if stage not in wanted:
            continue
        # An existing invalidation already makes this promotion historical;
        # the immutable record is never replaced with a newer reason.
        if (attempt_dir / "invalidated.json").exists():
            continue
        archived_path = history_path / CONTRACTS[stage].filename if history_path is not None else None
        if archived_path is not None and not archived_path.is_file():
            archived_path = None
        archived_hash = sha256_file(archived_path) if archived_path is not None else None
        promoted_hash = promoted.get("final_hash")
        record = {
            "schema_version": 1,
            "attempt_id": promoted.get("attempt_id"),
            "stage": stage,
            "reason": reason,
            "history_path": str(history_path) if history_path is not None else None,
            "archived_path": str(archived_path) if archived_path is not None else None,
            "promoted_hash": promoted_hash,
            "archived_hash": archived_hash,
            "hash_mismatch": archived_hash != promoted_hash,
        }
        write_json_exclusive(attempt_dir / "invalidated.json", record)
        invalidated.append(promoted.get("attempt_id"))
    return invalidated


def recover(task_dir, state, promote_func, stages=None, failed_writer_recovery=None):
    """Reconcile immutable attempt records. Return None or a blocking reason."""
    root = attempts_root(task_dir)
    if not root.exists():
        return None
    wanted = set(stages) if stages is not None else None
    records = []
    all_completed = []
    for attempt_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        try:
            dispatch = _load(attempt_dir / "dispatch.json")
        except Exception as exc:
            return "durable dispatch record is unreadable: %s" % exc
        stage = dispatch.get("stage")
        if wanted is not None and stage not in wanted:
            continue
        state.setdefault("attempts", {})[stage] = max(
            int(state.setdefault("attempts", {}).get(stage, 0)),
            int(dispatch.get("attempt_number") or 0),
        )
        approval = dispatch.get("approval") or {}
        if approval.get("consumed") and state.get("pending_approval", {}).get("approval_id") == approval.get("approval_id"):
            state["pending_approval"] = approval
        invalidated_path = attempt_dir / "invalidated.json"
        if invalidated_path.exists():
            try:
                invalidated = _load(invalidated_path)
            except Exception as exc:
                return "attempt %s invalidation record is unreadable: %s" % (dispatch.get("attempt_id"), exc)
            if (invalidated.get("attempt_id") != dispatch.get("attempt_id") or
                    invalidated.get("stage") != stage):
                return "attempt %s has an invalid invalidation record" % dispatch.get("attempt_id")
            continue
        completed_path = attempt_dir / "completed.json"
        if not completed_path.exists():
            return "attempt %s has a durable dispatch but no proven completion" % dispatch.get("attempt_id")
        try:
            completed = _load(completed_path)
        except Exception as exc:
            return "attempt completion record is unreadable: %s" % exc
        all_completed.append({"dispatch": dispatch, "completed": completed, "dir": attempt_dir})
        if not completed.get("eligible_for_promotion"):
            continue
        promoted = None
        promoted_path = attempt_dir / "promoted.json"
        if promoted_path.exists():
            try:
                promoted = _load(promoted_path)
            except Exception as exc:
                return "attempt promotion record is unreadable: %s" % exc
            if (promoted.get("attempt_id") != dispatch.get("attempt_id") or
                    promoted.get("stage") != stage or
                    promoted.get("final_hash") != completed.get("result_hash")):
                return "stage %s promotion record conflicts with its result" % stage
        records.append({
            "dispatch": dispatch, "completed": completed, "dir": attempt_dir,
            "promoted": promoted,
        })

    # A controller can die after recording a failed writer but before it
    # records the source-bound retry requirement or unchanged-source
    # resolution. A later attempt resolves it only when that dispatch contains
    # the matching consumed approval; mere dispatch order is not authority.
    if failed_writer_recovery is not None:
        for item in all_completed:
            dispatch = item["dispatch"]
            completed = item["completed"]
            if (dispatch.get("execution_mode") != "workspace-write" or
                    completed.get("eligible_for_promotion")):
                continue
            resolution_path = item["dir"] / "writer-resolution.json"
            if resolution_path.exists():
                try:
                    resolution = _load(resolution_path)
                except Exception as exc:
                    return "attempt %s writer resolution is unreadable: %s" % (dispatch.get("attempt_id"), exc)
                if (resolution.get("attempt_id") != dispatch.get("attempt_id") or
                        resolution.get("stage") != dispatch.get("stage")):
                    return "attempt %s has an invalid writer resolution" % dispatch.get("attempt_id")
                continue
            if _failed_writer_has_consumed_approval(dispatch, all_completed):
                continue
            reason = failed_writer_recovery(dispatch, completed)
            if reason:
                return reason

    # Only the newest promotion of a stage can remain authoritative.  Earlier
    # promotions are immutable history even when the canonical file was lost
    # after the later promotion.
    newest_promoted_attempt = {}
    for item in records:
        if item["promoted"] is None:
            continue
        stage = item["dispatch"].get("stage")
        number = int(item["dispatch"].get("attempt_number") or 0)
        newest_promoted_attempt[stage] = max(number, newest_promoted_attempt.get(stage, -1))

    successful = {}
    for item in records:
        dispatch = item["dispatch"]
        completed = item["completed"]
        attempt_dir = item["dir"]
        stage = dispatch.get("stage")
        attempt_number = int(dispatch.get("attempt_number") or 0)
        if attempt_number < newest_promoted_attempt.get(stage, attempt_number):
            continue

        recorded_hashes = dispatch.get("consumed_input_hashes")
        if not isinstance(recorded_hashes, dict):
            return "attempt %s lacks dispatch-time input provenance" % dispatch.get("attempt_id")
        provenance_complete, inputs_match = _dispatch_inputs_match(task_dir, stage, recorded_hashes)
        if not provenance_complete:
            return "attempt %s lacks complete dispatch-time input provenance" % dispatch.get("attempt_id")
        if not inputs_match:
            continue
        result_path = Path(completed.get("result_path", ""))
        if not result_path.is_file() or sha256_file(result_path) != completed.get("result_hash"):
            return "attempt %s has invalid or conflicting result evidence" % dispatch.get("attempt_id")
        output = result_path.read_text(encoding="utf-8")
        validation = validate_text(output, CONTRACTS[stage], read_only=True)
        if not validation["valid"]:
            return "attempt %s result no longer satisfies the stage contract" % dispatch.get("attempt_id")
        if item["promoted"] is not None:
            canonical = Path(task_dir) / CONTRACTS[stage].filename
            # A different current canonical artifact makes this an immutable
            # historical success, not authority to overwrite newer work.
            if canonical.exists() and sha256_file(canonical) != completed.get("result_hash"):
                continue
        prior = successful.get(stage)
        if prior and prior["completed"].get("result_hash") != completed.get("result_hash"):
            return "stage %s has conflicting successful durable attempts" % stage
        successful[stage] = {
            "dispatch": dispatch, "completed": completed, "output": output,
            "dir": attempt_dir, "consumed_input_hashes": recorded_hashes,
        }

    changed = False
    for stage, item in successful.items():
        dispatch = item["dispatch"]
        completed = item["completed"]
        final_path = Path(task_dir) / CONTRACTS[stage].filename
        if final_path.exists():
            if sha256_file(final_path) != completed.get("result_hash"):
                return "stage %s canonical artifact conflicts with its successful attempt" % stage
        else:
            final = promote_func(task_dir, stage, item["output"], read_only=True)
            if not final.get("finalized"):
                return "stage %s recovery could not promote its proven result" % stage
        promoted_path = item["dir"] / "promoted.json"
        if promoted_path.exists():
            promoted = _load(promoted_path)
            if promoted.get("final_hash") != completed.get("result_hash"):
                return "stage %s promotion record conflicts with its result" % stage
        else:
            write_json_exclusive(promoted_path, {
                "schema_version": 1, "attempt_id": dispatch.get("attempt_id"), "stage": stage,
                "final_path": str(final_path), "final_hash": completed.get("result_hash"),
            })
        run = dict(completed.get("result") or {})
        run.update({
            "attempt_id": dispatch.get("attempt_id"), "finalized": True,
            "final_artifact_path": str(final_path), "final_artifact_hash": completed.get("result_hash"),
        })
        # The normalized immutable result is the recovered candidate. This
        # avoids depending on a provider-specific transient output path after
        # a controller crash.
        run["candidate_artifact_path"] = completed.get("result_path")
        if stage == "05" and not run.get("dirty_baseline"):
            run["dirty_baseline"] = dispatch.get("implementation_baseline")
        runs = state.setdefault("real_stage_runs", {}).setdefault(stage, [])
        if not any(existing.get("attempt_id") == dispatch.get("attempt_id") for existing in runs if isinstance(existing, dict)):
            runs.append(run)
        state.setdefault("stage_agents", {})[stage] = dispatch.get("agent")
        state.setdefault("execution_modes", {})[stage] = dispatch.get("execution_mode")
        state.setdefault("durable_attempts", {})[dispatch.get("attempt_id")] = {
            "stage": stage, "attempt_number": dispatch.get("attempt_number"), "status": "promoted"
        }
        acknowledge_recorded_consumed_inputs(state, item["consumed_input_hashes"])
        changed = True
    if changed:
        write_state_atomic(task_dir, state)
    return None


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _optional_hash(path):
    try:
        candidate = Path(path)
        return sha256_file(candidate) if candidate.is_file() else None
    except Exception:
        return None


def _dispatch_inputs_match(task_dir, stage, recorded_hashes):
    expected_names = [CONTRACTS[key].filename for key in STAGE_CONSUMED_INPUTS.get(stage, [])]
    if (not isinstance(recorded_hashes, dict) or
            set(recorded_hashes) != set(expected_names) or
            any(not isinstance(recorded_hashes.get(name), str) for name in expected_names)):
        return False, False
    for name in expected_names:
        path = Path(task_dir) / name
        if not path.is_file() or sha256_file(path) != recorded_hashes[name]:
            return True, False
    return True, True


def _failed_writer_has_consumed_approval(failed_dispatch, all_completed):
    failed_number = failed_dispatch.get("attempt_number")
    failed_id = failed_dispatch.get("attempt_id")
    stage = failed_dispatch.get("stage")
    for item in all_completed:
        later = item["dispatch"]
        if (later.get("stage") != stage or
                int(later.get("attempt_number") or 0) <= int(failed_number or 0)):
            continue
        approval = later.get("approval") or {}
        matches = (
            approval.get("failed_attempt_id") == failed_id or
            approval.get("failed_attempt_number") == failed_number
        )
        if matches and approval.get("approved") and approval.get("consumed"):
            return True
    return False
