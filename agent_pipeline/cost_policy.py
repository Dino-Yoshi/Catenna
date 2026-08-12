"""Pure cost-control policy helpers."""

from __future__ import print_function

try:
    from collections.abc import Mapping
except ImportError:  # pragma: no cover - Python 2 compatibility fallback.
    from collections import Mapping


ALLOWED_STAGES = set(["02", "03", "04", "04_gate", "07"])


def compute_stage_overrides(config, ledger_entries, assignments=None, quality_entries=None):
    """Return {stage: {agent: {model/effort}}} for reliable candidates.

    ``assignments`` is accepted for compatibility with earlier sketches, but
    route correctness is handled by applying this agent-aware plan only after
    dispatch selection.
    """
    cost_control = config.get("cost_control", {})
    if not cost_control.get("enabled", False):
        return {}
    quality_entries = quality_entries or []
    roles = config.get("roles", {})
    candidates = cost_control.get("downgrade_candidates") or {}
    if not isinstance(candidates, Mapping):
        return {}
    overrides = {}
    for stage_key in cost_control.get("eligible_stages", []):
        if stage_key not in ALLOWED_STAGES:
            continue
        role = roles.get(stage_key) or {}
        if role.get("model_override") or role.get("effort_override"):
            continue
        stage_overrides = {}
        for agent, candidate in candidates.items():
            effective = _effective_candidate(candidate)
            if not effective:
                continue
            candidate_model = effective.get("model")
            if (
                _stage_eligible(config, stage_key, ledger_entries, agent, candidate_model)
                and _candidate_confirmed_safe(config, stage_key, ledger_entries, agent, candidate_model)
                and _candidate_confirmed_quality_safe(config, stage_key, quality_entries, agent, candidate_model)
            ):
                stage_overrides[agent] = effective
        if stage_overrides:
            overrides[stage_key] = stage_overrides
    return overrides


def _effective_candidate(candidate):
    if candidate is None or not isinstance(candidate, Mapping):
        return {}
    effective = {}
    if candidate.get("model") is not None:
        effective["model"] = candidate.get("model")
    if candidate.get("effort") is not None:
        effective["effort"] = candidate.get("effort")
    return effective


def _stage_eligible(config, stage_key, ledger_entries, agent, candidate_model=None):
    matching = [
        entry for entry in ledger_entries
        if entry.get("stage") == stage_key and entry.get("agent") == agent
        and (candidate_model is None or entry.get("model") != candidate_model)
    ]
    return _history_reliable(config, matching)


def _candidate_confirmed_safe(config, stage_key, ledger_entries, agent, candidate_model):
    if candidate_model is None:
        return True
    matching = [
        entry for entry in ledger_entries
        if entry.get("stage") == stage_key
        and entry.get("agent") == agent
        and entry.get("model") == candidate_model
    ]
    cost_control = config.get("cost_control", {})
    if len(matching) < int(cost_control.get("min_samples", 1)):
        return True
    return _history_reliable(config, matching)


def _candidate_confirmed_quality_safe(config, stage_key, quality_entries, agent, candidate_model):
    cost_control = config.get("cost_control", {})
    if not cost_control.get("quality_aware", False):
        return True
    if stage_key != "04":
        return True
    matching = [
        entry for entry in quality_entries
        if entry.get("stage") == "04"
        and entry.get("agent") == agent
        and entry.get("model") == candidate_model
    ]
    if len(matching) < int(cost_control.get("min_samples", 1)):
        return True
    return _rate_below_threshold(matching, lambda entry: not entry.get("accepted"), cost_control.get("max_rejection_rate", 0))


def _history_reliable(config, matching):
    cost_control = config.get("cost_control", {})
    if len(matching) < int(cost_control.get("min_samples", 1)):
        return False
    for entry in matching:
        if entry.get("failure_class") is not None:
            return False
    return _rate_below_threshold(matching, _is_retry, cost_control.get("max_retry_rate", 0))


def _rate_below_threshold(matching, is_bad, threshold):
    if not matching:
        return True
    bad = sum(1 for entry in matching if is_bad(entry))
    return (float(bad) / float(len(matching))) < float(threshold)


def _is_retry(entry):
    return _greater_than_one(entry.get("attempt_number")) or _greater_than_one(entry.get("pass_number"))


def _greater_than_one(value):
    try:
        return float(value) > 1
    except (TypeError, ValueError):
        return False
