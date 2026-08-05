"""Deterministic mock agents used by the controller."""

from __future__ import print_function

from .artifacts import CONTRACTS


def valid_artifact(stage_key):
    contract = CONTRACTS[stage_key]
    if stage_key == "00":
        return contract.heading + "\n\nImplement a deterministic mock pipeline orchestrator.\n"
    body = [contract.heading, ""]
    for section in contract.sections:
        body.append("## " + section)
        if section == "YAML gate":
            body.append("")
            body.append("```yaml")
            body.append("ready_for_implementation: true")
            body.append("blocking_issues: []")
            body.append("nonblocking_issues: []")
            body.append("required_revision_targets: []")
            body.append("```")
        elif section == "Decision" and stage_key in ("06", "08"):
            body.append("")
            body.append("- [x] Accept")
            body.append("- [ ] Reject")
            body.append("- [ ] Needs follow-up")
        else:
            body.append("")
            body.append("Mock content for " + section + ".")
        body.append("")
    if stage_key == "07":
        body.append("accept")
    return "\n".join(body).rstrip() + "\n"


def useful_partial(stage_key):
    contract = CONTRACTS[stage_key]
    if contract.gate:
        return contract.heading + "\n\n## Summary\n\nPartial but syntactically grounded.\n"
    return contract.heading + "\n\n## " + (contract.sections[0] if contract.sections else "Summary") + "\n\nPartial.\n"


def malformed(stage_key):
    return "Commentary before the required heading.\n\n" + CONTRACTS[stage_key].heading + "\n\nBad.\n"


def empty(stage_key):
    return ""


class MockAgent(object):
    def __init__(self, scenario):
        self.scenario = scenario or {}

    def outcome_for(self, stage_key, attempt):
        actions = self.scenario.get("actions", {})
        value = actions.get(stage_key, "success")
        if isinstance(value, list):
            if attempt - 1 < len(value):
                return value[attempt - 1]
            return value[-1]
        return value

    def invoke(self, agent, stage_key, attempt, completion_only=False):
        outcome = self.outcome_for(stage_key, attempt)
        if completion_only:
            if self.scenario.get("completion_retry_outcome") == "malformed_artifact":
                return {"agent": agent, "failure_class": "malformed_artifact", "output": malformed(stage_key)}
            outcome = "success"
        if outcome in ("success", "max_turns_complete"):
            return {"agent": agent, "failure_class": None, "output": valid_artifact(stage_key)}
        if outcome == "max_turns_useful_partial":
            return {"agent": agent, "failure_class": "max_turns", "output": useful_partial(stage_key)}
        if outcome == "max_turns_unusable":
            return {"agent": agent, "failure_class": "max_turns", "output": malformed(stage_key)}
        if outcome == "malformed_artifact":
            return {"agent": agent, "failure_class": "malformed_artifact", "output": malformed(stage_key)}
        if outcome == "empty_output":
            return {"agent": agent, "failure_class": "empty_output", "output": empty(stage_key)}
        if outcome == "malformed_gate":
            return {"agent": agent, "failure_class": None, "output": gate_artifact(stage_key, "ready_for_implementation true")}
        if outcome == "missing_gate_key":
            return {"agent": agent, "failure_class": None, "output": gate_artifact(stage_key, "ready_for_implementation: true\nblocking_issues: []\nnonblocking_issues: []")}
        if outcome == "wrong_gate_type":
            return {"agent": agent, "failure_class": None, "output": gate_artifact(stage_key, "ready_for_implementation: yes\nblocking_issues: []\nnonblocking_issues: []\nrequired_revision_targets: []")}
        if outcome == "usage_limit":
            return {"agent": agent, "failure_class": "usage_limit", "output": "", "reset_at": "2099-01-01T00:00:00Z"}
        if outcome == "rate_limit_with_reset":
            return {"agent": agent, "failure_class": "rate_limit", "output": "", "reset_at": "2099-01-01T00:00:00Z"}
        if outcome == "rate_limit_no_reset":
            return {"agent": agent, "failure_class": "rate_limit", "output": useful_partial(stage_key)}
        if outcome == "timeout":
            return {"agent": agent, "failure_class": "timeout", "output": useful_partial(stage_key)}
        if outcome == "process_interrupted":
            return {"agent": agent, "failure_class": "process_interrupted", "output": useful_partial(stage_key)}
        if outcome in ("permission_error", "sandbox_environment", "source_failure", "unknown_failure"):
            return {"agent": agent, "failure_class": outcome, "output": useful_partial(stage_key)}
        return {"agent": agent, "failure_class": "unknown_failure", "output": "unknown scenario outcome\n"}


def gate_artifact(stage_key, gate_body):
    contract = CONTRACTS[stage_key]
    lines = [contract.heading, ""]
    for section in contract.sections:
        lines.append("## " + section)
        lines.append("")
        if section == "YAML gate":
            lines.append("```yaml")
            lines.extend(gate_body.splitlines())
            lines.append("```")
        else:
            lines.append("Mock content for " + section + ".")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
