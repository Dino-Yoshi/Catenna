"""Routing and fallback policy for mock stages."""

from __future__ import print_function

ROLE_POLICY = {
    "02": {"role": "stage_2_spec_author", "primary": "codex", "fallbacks": ["agy", "claude"], "independent_from": None},
    "03": {"role": "stage_3_spec_audit", "primary": "codex", "fallbacks": ["agy", "claude"], "independent_from": None},
    "04": {"role": "stage_4_final_brief_author", "primary": "codex", "fallbacks": ["agy", "claude"], "independent_from": None},
    "04_gate": {"role": "stage_4_final_gate_reviewer", "primary": "claude", "fallbacks": ["agy"], "independent_from": "04"},
    "05": {"role": "stage_5_implementer", "primary": "codex", "fallbacks": ["agy", "claude"], "independent_from": None},
    "07": {"role": "stage_7_diff_reviewer", "primary": "claude", "fallbacks": ["agy"], "independent_from": "05"},
}

LOCAL_STAGES = {"00", "01", "06", "08"}


def choose_agent(stage_key, state, scenario, assignments):
    if stage_key in LOCAL_STAGES:
        return {"agent": "controller", "degraded": False, "fallback": False}
    policy = ROLE_POLICY[stage_key]
    safety_mode = scenario.get("safety_mode", "strict")
    candidates = [policy["primary"]] + list(policy["fallbacks"])
    unavailable = set(state.get("run_unavailable_agents") or {})
    for candidate in candidates:
        if candidate in unavailable:
            continue
        independent_from = policy.get("independent_from")
        if independent_from:
            prior = assignments.get(independent_from)
            if prior == candidate:
                if safety_mode == "continuity" and scenario.get("allow_degraded_same_agent_review"):
                    return {
                        "agent": candidate,
                        "degraded": True,
                        "fallback": candidate != policy["primary"],
                        "reason": "degraded_same_agent_review",
                    }
                continue
        return {"agent": candidate, "degraded": False, "fallback": candidate != policy["primary"]}
    return None
