"""Artifact contracts and small standard-library validators."""

from __future__ import print_function

import hashlib
import re
from collections import namedtuple

ArtifactContract = namedtuple(
    "ArtifactContract",
    "filename heading legacy_heading sections gate final_line body_required decision",
)


CONTRACTS = {
    "00": ArtifactContract(
        "00_original_request.md",
        "# Original request",
        None,
        [],
        None,
        None,
        True,
        False,
    ),
    "01": ArtifactContract(
        "01_requirements_packet.md",
        "# Stage 1 - Requirements / design packet",
        None,
        [
            "Objective",
            "Motivation",
            "Current behavior",
            "Desired behavior",
            "Constraints",
            "Non-goals",
            "Affected systems",
            "Rough implementation ideas",
            "Known risks",
            "Acceptance criteria",
        ],
        None,
        None,
        False,
        False,
    ),
    "02": ArtifactContract(
        "02_technical_spec.md",
        "# Stage 2 - Technical specification",
        None,
        [
            "Summary",
            "Source request",
            "Must-have requirements",
            "Nice-to-have requirements",
            "Non-goals",
            "Affected systems",
            "Proposed implementation shape",
            "Data/config/API changes",
            "Compatibility constraints",
            "Risks and edge cases",
            "Acceptance criteria",
            "Verification plan",
            "Open questions",
        ],
        None,
        None,
        False,
        False,
    ),
    "03": ArtifactContract(
        "03_audit.md",
        "# Stage 3 - Specification audit",
        None,
        [
            "Summary",
            "Blocking issues",
            "Nonblocking issues",
            "Compatibility risks",
            "Architecture concerns",
            "Implementation traps",
            "Required revision targets",
            "YAML gate",
        ],
        {
            "ready_for_implementation": bool,
            "blocking_issues": list,
            "nonblocking_issues": list,
            "required_revision_targets": list,
        },
        None,
        False,
        False,
    ),
    "04": ArtifactContract(
        "04_final_codex_brief.md",
        "# Stage 4 - Final implementation brief",
        "# Stage 4 - Final Codex implementation brief",
        [
            "Implementation objective",
            "Required behavior",
            "Explicit non-goals",
            "Files/classes likely involved",
            "Implementation constraints",
            "Edge cases to handle",
        ],
        None,
        None,
        False,
        False,
    ),
    "04_gate": ArtifactContract(
        "04_final_brief_audit.md",
        "# Stage 4 - Final brief audit",
        None,
        [
            "Summary",
            "Blocking issues",
            "Nonblocking issues",
            "Implementation risks",
            "Required brief revisions",
            "YAML gate",
        ],
        {
            "ready_for_implementation": bool,
            "blocking_issues": list,
            "nonblocking_issues": list,
            "required_revision_targets": list,
        },
        None,
        False,
        False,
    ),
    "05": ArtifactContract(
        "05_codex_implementation_report.md",
        "# Stage 5 - Implementation report",
        "# Stage 5 - Codex implementation report",
        [
            "Summary of changes",
            "Files changed",
            "Behavior implemented",
            "Verification performed",
            "Build/test results",
            "Deviations from brief",
            "Known limitations",
            "Follow-up recommendations",
        ],
        None,
        None,
        False,
        False,
    ),
    "06": ArtifactContract(
        "06_manual_test_notes.md",
        "# Stage 6 - Manual test notes",
        None,
        ["Decision"],
        None,
        None,
        False,
        False,
    ),
    "07": ArtifactContract(
        "07_diff_review.md",
        "# Stage 7 - Diff review",
        None,
        [
            "Summary",
            "Correctness findings",
            "Maintainability findings",
            "Regression risks",
            "Performance risks",
            "Brief compliance",
            "Required fixes",
            "Recommended follow-ups",
            "Verdict",
        ],
        None,
        "accept",
        False,
        False,
    ),
    "08": ArtifactContract(
        "08_decision.md",
        "# Stage 8 - Final decision",
        None,
        ["Decision", "Reason", "Follow-up task, if needed"],
        None,
        None,
        False,
        True,
    ),
}


def sha256_file(path):
    h = hashlib.sha256()
    with open(str(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_file(path, stage_key, read_only=False):
    contract = CONTRACTS[stage_key]
    if not path.exists():
        return {"valid": False, "reason": "missing", "failure_class": "source_failure"}
    data = path.read_text(encoding="utf-8")
    return validate_text(data, contract, read_only=read_only)


def validate_text(text, contract, read_only=False):
    if text == "":
        return {"valid": False, "reason": "empty output", "failure_class": "empty_output"}
    lines = text.splitlines()
    if not lines:
        return {"valid": False, "reason": "empty output", "failure_class": "empty_output"}
    first = lines[0].strip()
    accepted_heading = first == contract.heading
    if not accepted_heading and read_only and contract.legacy_heading:
        accepted_heading = first == contract.legacy_heading
    if not accepted_heading:
        if contract.heading in text or (contract.legacy_heading and contract.legacy_heading in text):
            return {
                "valid": False,
                "reason": "leading commentary before required heading",
                "failure_class": "malformed_artifact",
            }
        return {
            "valid": False,
            "reason": "missing or wrong top-level heading",
            "failure_class": "malformed_artifact",
        }
    if contract.body_required and not "\n".join(lines[1:]).strip():
        return {"valid": False, "reason": "body is empty", "failure_class": "malformed_artifact"}
    present_sections = set()
    for line in lines:
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            present_sections.add(match.group(1))
    missing_sections = []
    if contract.filename == "06_manual_test_notes.md":
        if "Decision" not in present_sections and "Overall manual result" not in present_sections:
            missing_sections.append("Decision or Overall manual result")
    else:
        for section in contract.sections:
            if section not in present_sections:
                missing_sections.append(section)
    if missing_sections:
        return {
            "valid": False,
            "reason": "missing sections: " + ", ".join(missing_sections),
            "failure_class": "malformed_artifact",
        }
    if contract.gate:
        gate_result = parse_gate(text)
        if not gate_result["valid"]:
            return gate_result
        gate = gate_result["gate"]
        for key, expected_type in contract.gate.items():
            if key not in gate:
                return {
                    "valid": False,
                    "reason": "missing gate key: " + key,
                    "failure_class": "malformed_artifact",
                }
            if not isinstance(gate[key], expected_type):
                return {
                    "valid": False,
                    "reason": "wrong gate value type: " + key,
                    "failure_class": "malformed_artifact",
                }
    if contract.final_line:
        non_empty = [line.strip() for line in lines if line.strip()]
        if not non_empty or non_empty[-1] not in ("accept", "reject", "needs_followup"):
            return {
                "valid": False,
                "reason": "invalid final verdict line",
                "failure_class": "malformed_artifact",
            }
    if contract.decision:
        checked = 0
        for label in ("Accept", "Reject", "Needs follow-up"):
            if re.search(r"^\s*-\s*\[[xX]\]\s+" + re.escape(label) + r"\s*$", text, re.M):
                checked += 1
        if checked != 1:
            return {
                "valid": False,
                "reason": "exactly one final decision checkbox must be checked",
                "failure_class": "malformed_artifact",
            }
    if contract.filename == "06_manual_test_notes.md":
        result = validate_manual_test_outcome(text)
        if not result["valid"]:
            return result
    return {"valid": True, "reason": "valid"}


def validate_manual_test_outcome(text):
    section = extract_last_section(text, ("Decision", "Overall manual result"))
    if section is None:
        return {
            "valid": False,
            "reason": "missing sections: Decision or Overall manual result",
            "failure_class": "malformed_artifact",
        }
    checked = len(re.findall(r"^\s*[-*+]\s*\[[xX]\]\s+(Accept|Reject|Needs follow-up)\s*$", section, re.M))
    if checked > 1:
        return {
            "valid": False,
            "reason": "exactly one manual decision checkbox must be checked",
            "failure_class": "malformed_artifact",
        }
    if checked == 1:
        return {"valid": True, "reason": "valid"}
    prose_lines = []
    for raw_line in section.splitlines():
        line = raw_line.strip()
        if not line or re.match(r"^[-*+]\s*\[[ xX]\]\s+", line):
            continue
        prose_lines.append(line)
    prose = " ".join(prose_lines)
    if explicit_manual_outcome(prose):
        return {"valid": True, "reason": "valid"}
    return {
        "valid": False,
        "reason": "manual test notes must state an explicit outcome",
        "failure_class": "malformed_artifact",
    }


def manual_test_decision(text):
    """Classify a Stage 6 manual test notes' stated outcome as
    "accept"/"reject"/"needs_followup". Only meaningful once
    validate_manual_test_outcome has already confirmed the text states an
    explicit outcome; returns None in the (should-not-happen-post-validation)
    case where no outcome can be determined."""
    section = extract_last_section(text, ("Decision", "Overall manual result")) or ""
    checked = re.findall(r"^\s*[-*+]\s*\[[xX]\]\s+(Accept|Reject|Needs follow-up)\s*$", section, re.M)
    if len(checked) == 1:
        label = checked[0]
        if label == "Reject":
            return "reject"
        if label == "Needs follow-up":
            return "needs_followup"
        return "accept"
    prose_lines = []
    for raw_line in section.splitlines():
        line = raw_line.strip()
        if not line or re.match(r"^[-*+]\s*\[[ xX]\]\s+", line):
            continue
        prose_lines.append(line)
    prose = " ".join(prose_lines)
    if re.search(r"\breject(?:ed|s)?\b|\bfail(?:ed|s)?\b|\bblocked\b", prose, re.I):
        return "reject"
    if re.search(r"\bneeds?\s+follow[- ]?up\b|\bfollow[- ]?up\s+required\b", prose, re.I):
        return "needs_followup"
    if re.search(r"\baccept(?:ed|s)?\b|\bpass(?:ed|es)?\b|\bapproved\b", prose, re.I):
        return "accept"
    return None


def extract_last_section(text, headings):
    lines = text.splitlines()
    selected = None
    capture = False
    collected = []
    for line in lines:
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            if capture:
                selected = "\n".join(collected)
            capture = match.group(1) in headings
            collected = []
            continue
        if capture:
            collected.append(line)
    if capture:
        selected = "\n".join(collected)
    return selected


def extract_section(text, headings):
    lines = text.splitlines()
    capture = False
    collected = []
    for line in lines:
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            if capture:
                break
            capture = match.group(1) in headings
            continue
        if capture:
            collected.append(line)
    if not capture and not collected:
        return None
    return "\n".join(collected)


def explicit_manual_outcome(prose):
    if not prose.strip():
        return False
    patterns = [
        r"\baccept(?:ed|s)?\b",
        r"\breject(?:ed|s)?\b",
        r"\bpass(?:ed|es)?\b",
        r"\bfail(?:ed|s)?\b",
        r"\bneeds?\s+follow[- ]?up\b",
        r"\bfollow[- ]?up\s+required\b",
        r"\bblocked\b",
        r"\bapproved\b",
    ]
    return any(re.search(pattern, prose, re.I) for pattern in patterns)


def useful_partial(text, contract):
    result = validate_text(text, contract)
    if result["valid"]:
        return False
    lines = text.splitlines()
    if not lines or lines[0].strip() != contract.heading:
        return False
    if contract.gate:
        gate = parse_gate(text)
        if gate["valid"]:
            return True
    for line in lines:
        if re.match(r"^##\s+.+", line):
            return True
    return False


def parse_gate(text):
    match = re.search(r"```ya?ml\s*\n(.*?)\n```", text, re.S | re.I)
    if not match:
        return {"valid": False, "reason": "missing yaml gate", "failure_class": "malformed_artifact"}
    gate = {}
    pending_key = None
    pending_items = None
    pending_indent = None

    def finish_pending():
        if pending_key is not None:
            gate[pending_key] = pending_items

    for raw_line in match.group(1).splitlines():
        if not raw_line.strip():
            continue
        if has_unquoted_hash(raw_line):
            return {"valid": False, "reason": "malformed gate syntax", "failure_class": "malformed_artifact"}
        line = raw_line.strip()
        if line.startswith("#"):
            return {"valid": False, "reason": "malformed gate syntax", "failure_class": "malformed_artifact"}
        if raw_line[:1].isspace():
            list_match = re.match(r"^(\s+)-\s+(.+?)\s*$", raw_line)
            if not list_match or pending_key is None:
                return {"valid": False, "reason": "malformed gate syntax", "failure_class": "malformed_artifact"}
            indent = len(list_match.group(1).replace("\t", "    "))
            if pending_indent is None:
                pending_indent = indent
            elif indent != pending_indent:
                return {"valid": False, "reason": "unsupported gate syntax", "failure_class": "malformed_artifact"}
            parsed = parse_list_item(list_match.group(2).strip())
            if parsed is _INVALID:
                return {"valid": False, "reason": "unsupported gate syntax", "failure_class": "malformed_artifact"}
            pending_items.append(parsed)
            continue
        if line.startswith("#") or ":" not in line:
            return {"valid": False, "reason": "malformed gate syntax", "failure_class": "malformed_artifact"}
        finish_pending()
        pending_key = None
        pending_items = None
        pending_indent = None
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            return {"valid": False, "reason": "malformed gate key", "failure_class": "malformed_artifact"}
        if value == "":
            pending_key = key
            pending_items = []
            pending_indent = None
            continue
        parsed = parse_scalar_or_array(value)
        if parsed is _INVALID:
            return {"valid": False, "reason": "unsupported gate syntax", "failure_class": "malformed_artifact"}
        gate[key] = parsed
    finish_pending()
    return {"valid": True, "gate": gate}


class _Invalid(object):
    pass


_INVALID = _Invalid()


def parse_scalar_or_array(value):
    if value == "true":
        return True
    if value == "false":
        return False
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        items = []
        for part in inner.split(","):
            parsed = parse_string(part.strip())
            if parsed is _INVALID:
                return _INVALID
            items.append(parsed)
        return items
    return parse_string(value)


def parse_list_item(value):
    if value == "" or value.startswith("-"):
        return _INVALID
    if not is_quoted(value) and ":" in value:
        return _INVALID
    return parse_string(value)


def parse_string(value):
    if value == "":
        return ""
    if value.startswith('"') and value.endswith('"') and value.count('"') == 2:
        return value[1:-1]
    if value.startswith("'") and value.endswith("'") and value.count("'") == 2:
        return value[1:-1]
    if re.match(r"^[A-Za-z0-9_ ./#:-]+$", value):
        return value
    return _INVALID


def is_quoted(value):
    return (
        (value.startswith('"') and value.endswith('"') and value.count('"') == 2)
        or (value.startswith("'") and value.endswith("'") and value.count("'") == 2)
    )


def has_unquoted_hash(value):
    quote = None
    for char in value:
        if char in ("'", '"'):
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
        elif char == "#" and quote is None:
            return True
    return False
