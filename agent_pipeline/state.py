"""Controller-owned state storage."""

from __future__ import print_function

import json
import os
import time

from .artifacts import CONTRACTS, sha256_file, validate_file
from .durable import DurableStorageError, atomic_write_json
from .failures import VALID_STATES

SCHEMA_VERSION = 2
STATE_KEYS = [
    "schema_version",
    "task",
    "run_id",
    "state",
    "current_stage",
    "completed_stages",
    "attempts",
    "artifact_status",
    "input_hashes",
    "agent_call_counts",
    "run_unavailable_agents",
    "unavailability_history",
    "human_checkpoint",
    "pending_approval",
    "last_failure",
    "fallback_events",
    "real_stage_runs",
    "stage_gate_passes",
    "stage_agents",
    "execution_modes",
    "manifest",
    "overseer",
    "fallback_history",
    "dirty_baseline",
    "next_required_human_action",
]

STAGE_ORDER = ["00", "01", "02", "03", "04", "04_gate", "05", "06", "07", "08"]

STAGE_CONSUMED_INPUTS = {
    "02": ["00", "01"],
    "03": ["02"],
    "04": ["03"],
    "04_gate": ["04"],
    "05": ["04_gate"],
    "06": ["05"],
    "07": ["06"],
    "08": ["07"],
}


class CorruptState(Exception):
    pass


def orchestrator_dir(task_dir):
    return task_dir / ".orchestrator"


def state_path(task_dir):
    return orchestrator_dir(task_dir) / "state.json"


def new_state(task, run_id=None):
    return {
        "schema_version": SCHEMA_VERSION,
        "task": task,
        "run_id": run_id,
        "state": "ready",
        "current_stage": None,
        "completed_stages": [],
        "attempts": {},
        "artifact_status": {},
        "input_hashes": {},
        "agent_call_counts": {},
        "run_unavailable_agents": {},
        "unavailability_history": [],
        "human_checkpoint": None,
        "pending_approval": None,
        "last_failure": None,
        "fallback_events": [],
        "real_stage_runs": {},
        "stage_gate_passes": [],
        "stage_agents": {},
        "execution_modes": {},
        "manifest": None,
        "overseer": None,
        "fallback_history": [],
        "dirty_baseline": None,
        "next_required_human_action": None,
    }


def load_state(task_dir, task):
    path = state_path(task_dir)
    if not path.exists():
        return new_state(task)
    try:
        with open(str(path), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:
        raise CorruptState("state JSON is unreadable: " + str(exc))
    version = data.get("schema_version")
    if version not in (1, SCHEMA_VERSION):
        raise CorruptState("unsupported state schema_version")
    required_v1 = STATE_KEYS[: STATE_KEYS.index("real_stage_runs")]
    missing = [key for key in required_v1 if key not in data]
    if missing:
        raise CorruptState("state is missing keys: " + ", ".join(missing))
    if version == 1:
        data = migrate_v1_to_v2(data)
    else:
        missing = [key for key in STATE_KEYS if key not in data]
        if missing:
            raise CorruptState("state is missing keys: " + ", ".join(missing))
    if data.get("state") not in VALID_STATES:
        raise CorruptState("invalid state value: " + repr(data.get("state")))
    if data.get("task") != task:
        raise CorruptState("state task does not match requested task")
    return data


def migrate_v1_to_v2(data):
    migrated = dict(data)
    migrated["schema_version"] = SCHEMA_VERSION
    defaults = new_state(migrated.get("task"), migrated.get("run_id"))
    for key in STATE_KEYS:
        if key not in migrated:
            migrated[key] = defaults[key]
    return migrated


def write_state_atomic(task_dir, state):
    atomic_write_json(state_path(task_dir), state)


def append_log(task_dir, event):
    directory = orchestrator_dir(task_dir)
    payload = dict(event)
    payload.setdefault("timestamp", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with open(str(directory / "log.jsonl"), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception as exc:
        raise DurableStorageError("cannot durably append controller log: %s" % exc)


def reconcile_artifacts(task_dir, state, read_only=False):
    view = artifact_view(task_dir, state, read_only=read_only)
    previous_hashes = view["previous_hashes"]
    invalidated = view["invalidated"]
    completed = view["completed"]
    state["artifact_status"] = view["artifact_status"]
    if not read_only:
        if invalidated:
            state["input_hashes"] = previous_hashes
        elif not previous_hashes or view["stale_from"] or set(completed) == set(STAGE_ORDER):
            state["input_hashes"] = view["current_hashes"]
    state["completed_stages"] = completed
    state["current_stage"] = view["current_stage"]
    if state["current_stage"] is None:
        state["state"] = "complete"
    elif state.get("state") == "complete":
        state["state"] = "ready"
    return invalidated


def artifact_view(task_dir, state, read_only=True):
    """The reconcile rule, computed without mutating `state`.

    An artifact whose hash differs from its acknowledged hash invalidates every
    stage downstream of it; completion is the contiguous run of structurally
    valid, non-invalidated artifacts, and `current_stage` is the first stage
    `run` would dispatch.
    """
    previous_hashes = dict(state.get("input_hashes") or {})
    artifact_status = {}
    current_hashes = {}
    structurally_valid = []
    stale_from = None
    for key in STAGE_ORDER:
        contract = CONTRACTS[key]
        path = task_dir / contract.filename
        if path.exists():
            digest = sha256_file(path)
            current_hashes[contract.filename] = digest
            validation = validate_file(path, key, read_only=read_only)
            artifact_status[contract.filename] = {
                "stage": key,
                "hash": digest,
                "status": "valid" if validation["valid"] else "invalid",
                "reason": validation["reason"],
            }
            if previous_hashes.get(contract.filename) and previous_hashes.get(contract.filename) != digest:
                stale_from = stale_from or key
            if validation["valid"]:
                structurally_valid.append(key)
        else:
            artifact_status[contract.filename] = {
                "stage": key,
                "status": "missing",
                "reason": "missing",
            }
    invalidated = invalidated_from(stale_from) if stale_from else []
    if invalidated:
        for key in invalidated:
            filename = CONTRACTS[key].filename
            if filename in artifact_status:
                artifact_status[filename]["stale"] = True
    effectively_valid = [key for key in structurally_valid if key not in invalidated]
    completed = contiguous_completed(effectively_valid)
    return {
        "previous_hashes": previous_hashes,
        "current_hashes": current_hashes,
        "artifact_status": artifact_status,
        "stale_from": stale_from,
        "invalidated": invalidated,
        "completed": completed,
        "current_stage": next_stage(completed),
    }


def upstream_staleness(task_dir, state, before_stage):
    """Read-only: whether the task artifacts upstream of `before_stage` still
    match the chain the controller acknowledged (F01).

    Returns the offending artifacts (missing, invalid, unacknowledged, or
    changed since acknowledgement) and the first stage `run` would dispatch,
    both derived from `artifact_view`, the same rule `reconcile_artifacts`
    applies.
    """
    view = artifact_view(task_dir, state, read_only=True)
    acknowledged = view["previous_hashes"]
    upstream = STAGE_ORDER[:STAGE_ORDER.index(before_stage)]
    problems = []
    for key in upstream:
        filename = CONTRACTS[key].filename
        detail = view["artifact_status"].get(filename) or {}
        recorded = acknowledged.get(filename)
        if detail.get("status") == "missing":
            problems.append((filename, "missing"))
        elif not isinstance(recorded, str):
            problems.append((filename, "unacknowledged"))
        elif detail.get("hash") != recorded:
            problems.append((filename, "changed"))
        elif detail.get("status") != "valid":
            problems.append((filename, "invalid: %s" % detail.get("reason")))
    redispatch = view["current_stage"]
    precedes = redispatch in upstream
    return {
        "stale": bool(problems) or precedes,
        "artifacts": problems,
        "redispatch_stage": redispatch,
    }


def acknowledge_consumed_inputs(task_dir, state, stage_key):
    acknowledged = []
    input_hashes = dict(state.get("input_hashes") or {})
    for consumed_stage in STAGE_CONSUMED_INPUTS.get(stage_key, []):
        contract = CONTRACTS[consumed_stage]
        path = task_dir / contract.filename
        if not path.exists():
            continue
        input_hashes[contract.filename] = sha256_file(path)
        acknowledged.append(consumed_stage)
    state["input_hashes"] = input_hashes
    return acknowledged


def acknowledge_recorded_consumed_inputs(state, recorded_hashes):
    """Acknowledge hashes already captured by an immutable dispatch record.

    Recovery must not turn whatever happens to be on disk at resume time into
    the inputs a completed attempt is said to have consumed.
    """
    input_hashes = dict(state.get("input_hashes") or {})
    input_hashes.update(dict(recorded_hashes or {}))
    state["input_hashes"] = input_hashes


def contiguous_completed(valid_stage_keys):
    valid = set(valid_stage_keys)
    completed = []
    for key in STAGE_ORDER:
        if key not in valid:
            break
        completed.append(key)
    return completed


def invalidated_from(stage_key):
    if stage_key in ("00", "01"):
        return ["02", "03", "04", "04_gate", "05", "06", "07", "08"]
    if stage_key == "02":
        return ["03", "04", "04_gate", "05", "06", "07", "08"]
    if stage_key == "03":
        return ["04", "04_gate", "05", "06", "07", "08"]
    if stage_key == "04":
        return ["04_gate", "05", "06", "07", "08"]
    if stage_key == "04_gate":
        return ["05", "06", "07", "08"]
    if stage_key == "05":
        return ["06", "07", "08"]
    if stage_key == "06":
        return ["07", "08"]
    if stage_key == "07":
        return ["08"]
    return []


def next_stage(completed):
    completed_set = set(completed)
    for key in STAGE_ORDER:
        if key not in completed_set:
            return key
    return None
