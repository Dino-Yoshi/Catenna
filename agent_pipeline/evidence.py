"""Stage 6-8 evidence bound to source identity and consumed inputs (C06).

Evidence records live in `state["evidence"]` as append-only lists, one per
kind. Currency is always judged from the newest applicable record: an older
passing record is never selected to restore acceptance once a newer
failed, interrupted, unbound, or invalidated record exists.
"""

from __future__ import print_function

import hashlib
import json
import shutil
import time
import uuid
from pathlib import Path

from .artifacts import CONTRACTS, manual_test_decision, sha256_file
from .config import agent_config, configured_candidates
from .source_identity import identity_problem, parse_cited_identity, same_identity, with_identity_change
from .state import orchestrator_dir, upstream_staleness


EVIDENCE_KINDS = ("verification", "stage06", "stage07", "stage08")
VERIFICATION_INPUT_KEYS = ("brief", "gate", "stage5_report", "verification_config")
MANUAL_STAGE06_INPUT_KEYS = ("brief", "gate", "stage5_report")
REVIEW_INPUT_KEYS = ("brief", "gate", "stage5_report", "review_config")
# Files that Stage 5 post-processing and verification regenerate.
POSTPROCESSING_FILES = (
    "05_implementation_manifest.json",
    "05_verification_report.json",
    "05_verification_report.md",
    "05_supervisor_handoff.json",
    "05_supervisor_handoff.md",
    "handoff.md",
)


def store(state):
    evidence = state.get("evidence")
    if not isinstance(evidence, dict):
        evidence = {}
        state["evidence"] = evidence
    for kind in EVIDENCE_KINDS:
        if not isinstance(evidence.get(kind), list):
            evidence[kind] = []
    return evidence


def append(state, kind, entry):
    record = dict(entry)
    record["record_id"] = uuid.uuid4().hex
    record["recorded_at"] = now()
    store(state)[kind].append(record)
    return record


def records(state, kind):
    evidence = state.get("evidence")
    if not isinstance(evidence, dict) or not isinstance(evidence.get(kind), list):
        return []
    return [record for record in evidence[kind] if isinstance(record, dict)]


def latest(state, kind):
    items = records(state, kind)
    return items[-1] if items else None


def artifact_hash(task_dir, stage_key):
    path = task_dir / CONTRACTS[stage_key].filename
    if not path.exists() or not path.is_file():
        return None
    return sha256_file(path)


def _config_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def verification_config_hash(config, run_build=False):
    config = config or {}
    return _config_digest({
        "verification": config.get("verification", {}),
        "enable_auto_verified": config.get("enable_auto_verified", True),
        "run_build": bool(run_build),
    })


def review_config_hash(config):
    config = config or {}
    try:
        candidates = configured_candidates(config, "07")
    except Exception:
        candidates = []
    agents = {}
    for name in candidates:
        try:
            agents[name] = agent_config(config, name)
        except Exception:
            agents[name] = None
    return _config_digest({
        "role": (config.get("roles") or {}).get("07"),
        "agents": agents,
        "turn_budget": (config.get("turn_budgets") or {}).get("07"),
        "default_safety_mode": config.get("default_safety_mode"),
        "allow_degraded_same_agent_review": config.get("allow_degraded_same_agent_review"),
        "cost_control": config.get("cost_control"),
    })


def current_inputs(task_dir, config, run_build=False):
    return {
        "brief": artifact_hash(task_dir, "04"),
        "gate": artifact_hash(task_dir, "04_gate"),
        "stage5_report": artifact_hash(task_dir, "05"),
        "verification_config": verification_config_hash(config, run_build=run_build),
        "review_config": review_config_hash(config),
    }


def subset(inputs, keys):
    return {key: (inputs or {}).get(key) for key in keys}


def review_input_identity(task_dir, state, identity, inputs, config=None):
    """Return the stable R02 identity for the inputs to a Stage 7 review.

    The Stage 6 hash comes from its bound evidence record, rather than merely
    from a file which happens to have the Stage 6 name.  That keeps an
    unbound/manual artifact from acquiring review authority by presence alone.
    """
    fingerprint = (identity or {}).get("fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        return {"identity": None, "reason": "current source fingerprint is unavailable"}
    review_inputs = subset(inputs, REVIEW_INPUT_KEYS)
    missing = [key for key in REVIEW_INPUT_KEYS if not isinstance(review_inputs.get(key), str)]
    if missing:
        return {"identity": None, "reason": "review inputs are incomplete: " + ", ".join(missing)}
    stage06_check = stage06_status(task_dir, state, identity, inputs, config=config)
    if not stage06_check.get("current"):
        return {"identity": None, "reason": "Stage 6 evidence is not current: " + stage06_check.get("reason", "unknown reason")}
    stage06 = stage06_check.get("record") or {}
    stage06_hash = stage06.get("artifact_hash")
    if not isinstance(stage06_hash, str) or artifact_hash(task_dir, "06") != stage06_hash:
        return {"identity": None, "reason": "bound Stage 6 artifact is missing or changed"}
    payload = {
        "source_fingerprint": fingerprint,
        "review_inputs": review_inputs,
        "stage06_artifact_hash": stage06_hash,
    }
    return {
        "identity": _config_digest(payload),
        "reason": "review-input identity is current",
        "components": payload,
    }


def _changed_inputs(recorded, current, keys):
    recorded = recorded if isinstance(recorded, dict) else {}
    return [key for key in keys if key not in recorded or recorded.get(key) != current.get(key)]


def _result(current, reason, **extra):
    result = {"current": bool(current), "reason": reason}
    result.update(extra)
    return result


def _source_mismatch(recorded, identity):
    problem = identity_problem(recorded)
    if problem is not None:
        return "recorded " + problem
    if not same_identity(recorded, identity):
        return with_identity_change("source identity changed since this evidence was recorded", recorded, identity)
    return None


def verification_status(state, identity, inputs, record=None):
    """Whether `record` (default: the newest) is a passing verification bound
    to the current source and inputs and not superseded by a newer attempt
    against those same inputs."""
    items = records(state, "verification")
    if record is None:
        record = items[-1] if items else None
    if record is None:
        return _result(False, "no bound verification evidence is recorded")
    if record.get("status") != "passed":
        return _result(False, "verification status is %s" % record.get("status"), record=record)
    if not record.get("source_current"):
        return _result(False, record.get("source_reason") or "verification source identity is not bound", record=record)
    mismatch = _source_mismatch(record.get("source_after"), identity)
    if mismatch:
        return _result(False, "verification: " + mismatch, record=record)
    if not same_identity(record.get("source_before"), record.get("source_after")):
        return _result(False, with_identity_change("verification source identity changed during checks", record.get("source_before"), record.get("source_after")), record=record)
    changed = _changed_inputs(record.get("inputs"), inputs, VERIFICATION_INPUT_KEYS)
    if changed:
        return _result(False, "verification inputs changed: " + ", ".join(changed), record=record)
    try:
        position = items.index(record)
    except ValueError:
        return _result(False, "verification record is not in the evidence history", record=record)
    for later in items[position + 1:]:
        if _same_verification_inputs(later, identity, inputs) and later.get("status") != "passed":
            return _result(False, "superseded by a later %s verification of the same inputs" % later.get("status"), record=record)
    return _result(True, "verification is bound to the current source and inputs", record=record)


def _same_verification_inputs(record, identity, inputs):
    source = record.get("source_before")
    if identity_problem(source) is not None:
        # An attempt whose starting identity is unknown cannot be ruled out.
        return True
    return same_identity(source, identity) and not _changed_inputs(record.get("inputs"), inputs, VERIFICATION_INPUT_KEYS)


def _configured_check_names(config):
    commands = (((config or {}).get("verification") or {}).get("driven_project_commands") or [])
    return [str(command.get("name")) for command in commands if isinstance(command, dict) and command.get("name")]


def _manual_stage06_decision(task_dir):
    try:
        text = (task_dir / CONTRACTS["06"].filename).read_text(encoding="utf-8")
    except Exception:
        return None
    return manual_test_decision(text)


def manual_acceptance_verification_status(task_dir, state, config, identity, inputs):
    """R05 eligibility for a manual Accept.

    Non-accepting manual decisions and configurations without driven-project
    commands deliberately retain the existing manual-only behavior.
    """
    names = _configured_check_names(config)
    if not names or _manual_stage06_decision(task_dir) != "accept":
        return _result(True, "manual decision does not require configured verification")
    check = verification_status(state, identity, inputs)
    if check["current"]:
        return check
    record = check.get("record")
    if record is None:
        condition = "missing verification evidence"
    elif record.get("status") == "interrupted":
        condition = "interrupted verification evidence"
    elif record.get("status") != "passed":
        condition = "failing verification evidence"
    else:
        condition = "unbound verification evidence"
    label = "configured driven-project check" if len(names) == 1 else "configured driven-project checks"
    reason = (
        "manual Accept requires %s %s, but it has %s (%s); "
        "fix the source and re-run `catenna verify`, or record a non-accepting decision"
        % (label, ", ".join("`%s`" % name for name in names), condition, check["reason"])
    )
    return _result(False, reason, record=record)


def stage06_status(task_dir, state, identity, inputs, config=None):
    digest = artifact_hash(task_dir, "06")
    if digest is None:
        return _result(False, "Stage 6 notes are missing", binding="missing")
    record = latest(state, "stage06")
    if record is None or record.get("status") != "bound" or record.get("artifact_hash") != digest:
        return _result(False, "Stage 6 notes are not bound to recorded evidence", binding="unbound", record=record)
    mismatch = _source_mismatch(record.get("source"), identity)
    if mismatch:
        return _result(False, "Stage 6: " + mismatch, binding="stale", record=record)
    if record.get("route") == "auto":
        changed = _changed_inputs(record.get("inputs"), inputs, VERIFICATION_INPUT_KEYS)
        if changed:
            return _result(False, "Stage 6 inputs changed: " + ", ".join(changed), binding="stale", record=record)
        verification = None
        for item in records(state, "verification"):
            if item.get("record_id") == record.get("verification_record_id"):
                verification = item
        if verification is None:
            return _result(False, "Stage 6 automatic evidence has no verification record", binding="stale", record=record)
        check = verification_status(state, identity, inputs, record=verification)
        if not check["current"]:
            return _result(False, "Stage 6 automatic evidence: " + check["reason"], binding="stale", record=record)
    else:
        changed = _changed_inputs(record.get("inputs"), inputs, MANUAL_STAGE06_INPUT_KEYS)
        if changed:
            return _result(False, "Stage 6 inputs changed: " + ", ".join(changed), binding="stale", record=record)
        check = manual_acceptance_verification_status(task_dir, state, config, identity, inputs)
        if not check["current"]:
            return _result(False, check["reason"], binding="stale", record=record)
    return _result(True, "Stage 6 evidence is current", binding="current", record=record)


def manual_citation_problem(task_dir, state, identity):
    """None when the Stage 6 notes explicitly cite exactly the current source
    identity, else the correction the operator must make."""
    path = task_dir / CONTRACTS["06"].filename
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        return "Stage 6 notes are unreadable: %s" % exc
    digest = sha256_file(path)
    for record in records(state, "stage06"):
        if record.get("route") == "auto" and record.get("artifact_hash") == digest:
            return "Stage 6 notes are a controller-generated automatic record; they cannot be adopted as manual evidence"
    cited, sections = parse_cited_identity(text)
    wanted = "sha256:" + identity["fingerprint"]
    if sections == 0 or not cited:
        return "Stage 6 notes do not cite a source identity; retest and add a `## Source identity` section containing %s" % wanted
    if len(cited) != 1:
        return "Stage 6 notes cite more than one source identity; keep exactly one (current: %s)" % wanted
    if cited[0] != identity["fingerprint"]:
        return "Stage 6 notes cite sha256:%s but the current source identity is %s; retest the current source and update the notes" % (cited[0], wanted)
    return None


def stage07_status(task_dir, state, identity, inputs):
    digest = artifact_hash(task_dir, "07")
    if digest is None:
        return _result(False, "Stage 7 review is missing")
    record = latest(state, "stage07")
    if record is None:
        return _result(False, "Stage 7 review is not bound to recorded evidence")
    if record.get("status") != "passed":
        return _result(False, "latest Stage 7 review attempt is %s" % record.get("status"), record=record)
    if record.get("artifact_hash") != digest:
        return _result(False, "Stage 7 review does not match its recorded evidence", record=record)
    stage06 = latest(state, "stage06")
    if not stage06 or record.get("stage06_record_id") != stage06.get("record_id"):
        return _result(False, "Stage 7 review predates the current Stage 6 evidence", record=record)
    if not same_identity(record.get("source_before"), record.get("source_after")):
        return _result(False, with_identity_change("source identity changed during Stage 7 review", record.get("source_before"), record.get("source_after")), record=record)
    mismatch = _source_mismatch(record.get("source_after"), identity)
    if mismatch:
        return _result(False, "Stage 7: " + mismatch, record=record)
    changed = _changed_inputs(record.get("inputs"), inputs, REVIEW_INPUT_KEYS)
    if changed:
        return _result(False, "Stage 7 inputs changed: " + ", ".join(changed), record=record)
    return _result(True, "Stage 7 review is current", record=record)


def stage08_status(task_dir, state, identity):
    digest = artifact_hash(task_dir, "08")
    if digest is None:
        return _result(False, "Stage 8 decision is missing")
    record = latest(state, "stage08")
    if record is None or record.get("status") != "recorded":
        return _result(False, "Stage 8 decision is not bound to recorded evidence (legacy or superseded)", record=record)
    if record.get("artifact_hash") != digest:
        return _result(False, "Stage 8 decision does not match its recorded evidence", record=record)
    stage06 = latest(state, "stage06")
    stage07 = latest(state, "stage07")
    if not stage06 or record.get("stage06_record_id") != stage06.get("record_id"):
        return _result(False, "Stage 8 decision predates the current Stage 6 evidence", record=record)
    if not stage07 or record.get("stage07_record_id") != stage07.get("record_id"):
        return _result(False, "Stage 8 decision predates the current Stage 7 review", record=record)
    mismatch = _source_mismatch(record.get("source"), identity)
    if mismatch:
        return _result(False, "Stage 8: " + mismatch, record=record)
    return _result(True, "Stage 8 decision is current", record=record)


def evaluate(task_dir, state, config, identity, identity_error=None):
    """Read-only summary of current eligibility, for status/dry-run/report."""
    if identity is None or identity_problem(identity) is not None:
        reason = "source identity unavailable: %s" % (identity_error or identity_problem(identity))
        return {
            "source_identity": None,
            "source_error": reason,
            "verification": _result(False, reason),
            "stage06": _result(False, reason),
            "stage07": _result(False, reason),
            "stage08": _result(False, reason),
            "current_acceptance": False,
            "reason": reason,
        }
    inputs = current_inputs(task_dir, config)
    result = {
        "source_identity": identity,
        "source_error": None,
        "verification": verification_status(state, identity, inputs),
        "stage06": stage06_status(task_dir, state, identity, inputs, config=config),
        "stage07": stage07_status(task_dir, state, identity, inputs),
        "stage08": stage08_status(task_dir, state, identity),
    }
    # Stage 6-8 can be internally source-current while an upstream task
    # artifact has changed.  The acknowledged hashes intentionally remain at
    # the accepted chain until the dependent stage is rerun; do not present
    # that historical evidence as current acceptance in the meantime.  F01:
    # the whole accepted chain (00-05), judged by the reconcile rule itself.
    upstream = upstream_staleness(Path(task_dir), state, "06")
    if upstream["stale"]:
        reason = upstream_reason(upstream)
        for key in ("stage06", "stage07", "stage08"):
            result[key] = _result(False, reason)
    for key in ("stage06", "stage07", "stage08"):
        if not result[key]["current"]:
            result["current_acceptance"] = False
            result["reason"] = result[key]["reason"]
            break
    else:
        decision = (result["stage08"].get("record") or {}).get("decision")
        result["current_acceptance"] = decision == "accept"
        result["reason"] = "current final decision: %s" % decision
    for key in ("verification", "stage06", "stage07", "stage08"):
        result[key] = {k: v for k, v in result[key].items() if k != "record"}
    return result


def upstream_reason(upstream):
    named = ", ".join("%s (%s)" % item for item in upstream["artifacts"]) or "none named"
    redispatch = upstream["redispatch_stage"]
    if redispatch is not None:
        next_step = "`run` will re-dispatch Stage %s" % redispatch
    else:
        next_step = "`run` will re-acknowledge the accepted chain before any new acceptance"
    return "upstream task artifacts differ from the accepted chain: %s; %s" % (named, next_step)


def archive(task_dir, filenames, reason, run_id=None):
    """Copy existing task files into a new history directory. Returns the
    archived file names and the directory (or None if nothing existed)."""
    present = [name for name in filenames if (task_dir / name).is_file()]
    if not present:
        return [], None
    directory = orchestrator_dir(task_dir) / "history" / ("%s-%s" % (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), uuid.uuid4().hex[:8]))
    directory.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for name in present:
        shutil.copy2(str(task_dir / name), str(directory / name))
        hashes[name] = sha256_file(directory / name)
    (directory / "reason.json").write_text(
        json.dumps({"reason": reason, "run_id": run_id, "archived_at": now(), "files": hashes}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return present, directory


def remove(task_dir, filenames):
    for name in filenames:
        path = Path(task_dir) / name
        if path.is_file():
            path.unlink()


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
