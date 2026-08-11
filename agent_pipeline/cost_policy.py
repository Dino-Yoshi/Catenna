"""Pure cost-control policy helpers."""

from __future__ import print_function

try:
    from collections.abc import Mapping
except ImportError:  # pragma: no cover - Python 2 compatibility fallback.
    from collections import Mapping


ALLOWED_STAGES = set(["02", "03", "04", "04_gate", "07"])


def compute_stage_overrides(config, ledger_entries, assignments=None):
    """Return {stage: {agent: {model/effort}}} for reliable candidates.

    ``assignments`` is accepted for compatibility with earlier sketches, but
    route correctness is handled by applying this agent-aware plan only after
    dispatch selection.
    """
    cost_control = config.get("cost_control", {})
    if not cost_control.get("enabled", False):
        return {}
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
            if _stage_eligible(config, stage_key, ledger_entries, agent):
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


def _stage_eligible(config, stage_key, ledger_entries, agent):
    cost_control = config.get("cost_control", {})
    matching = [
        entry for entry in ledger_entries
        if entry.get("stage") == stage_key and entry.get("agent") == agent
    ]
    if len(matching) < int(cost_control.get("min_samples", 1)):
        return False
    for entry in matching:
        if entry.get("failure_class") is not None:
            return False
    retry_count = 0
    for entry in matching:
        if _is_retry(entry):
            retry_count += 1
    retry_rate = float(retry_count) / float(len(matching))
    return retry_rate < float(cost_control.get("max_retry_rate", 0))


def _is_retry(entry):
    return _greater_than_one(entry.get("attempt_number")) or _greater_than_one(entry.get("pass_number"))


def _greater_than_one(value):
    try:
        return float(value) > 1
    except (TypeError, ValueError):
        return False
