"""Limited post-implementation handoff generation."""

from __future__ import print_function

import json
import time


ALLOWED_ROUTES = set(["manual_test", "blocked", "administrator_action", "auto_verified"])


def parse_overseer_candidate(text):
    data = text if isinstance(text, dict) else json.loads(text)
    if data.get("route") not in ALLOWED_ROUTES:
        raise ValueError("unknown handoff route")
    for key in ("summary", "verified", "needs_human_testing", "known_limitations"):
        if not isinstance(data.get(key), list):
            raise ValueError("handoff field must be a list: " + key)
    if not isinstance(data.get("next_action"), str) or not data.get("next_action").strip():
        raise ValueError("handoff next_action is required")
    return data


def fallback_handoff(manifest, reason, verification_report=None):
    changed = [item.get("path") for item in manifest.get("changed_files", [])]
    return {
        "route": "manual_test",
        "summary": ["Stage 5 completed and the controller generated a deterministic handoff."],
        "verified": verification_summary_bullets(verification_report),
        "needs_human_testing": ["Run the manual Stage 6 validation for the implementation scope."],
        "known_limitations": ["Overseer output was unavailable or invalid: " + reason],
        "next_action": "Create Stage 6 manual test notes for the implemented changes.",
        "changed_files": changed,
        "generated_at": now(),
        "fallback": True,
    }


def verification_summary_bullets(verification_report):
    if not verification_report:
        return ["No automatic verification was marked passed by the controller."]
    result = ["%s: %s" % (check.get("name"), check.get("status")) for check in verification_report.get("checks", [])]
    signal = verification_report.get("test_coverage_delta_signal") or {}
    if signal.get("status"):
        result.append("test_coverage_delta_signal: %s" % signal["status"])
    return result or ["No automatic verification was marked passed by the controller."]


def upgrade_to_auto_verified(handoff, verification_report):
    """Deterministically upgrade a handoff's route to auto_verified.

    Callers (run_real_pipeline) are responsible for only invoking this once
    verification evidence actually qualifies -- this function does not
    re-check the report itself. It only refuses to override an existing
    blocked/administrator_action route: an explicit block always outranks
    evidence that merely looks clean, so a route decided by the overseer (or
    its deterministic fallback) as unsafe can never be silently upgraded."""
    if handoff.get("route") in ("blocked", "administrator_action"):
        return handoff
    upgraded = dict(handoff)
    upgraded["route"] = "auto_verified"
    upgraded["verified"] = verification_summary_bullets(verification_report)
    upgraded["known_limitations"] = list(handoff.get("known_limitations") or []) + [
        "Stage 6 was completed automatically from build/test evidence; no human played the mod in-game for this task."
    ]
    upgraded["next_action"] = "Stage 7 diff review and Stage 8 decision will run automatically."
    return upgraded


def write_handoff_files(task_dir, handoff, source):
    json_path = task_dir / "05_supervisor_handoff.json"
    md_path = task_dir / "05_supervisor_handoff.md"
    legacy_path = task_dir / "handoff.md"
    payload = dict(handoff)
    payload["source"] = source
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown = render_markdown(payload)
    md_path.write_text(markdown, encoding="utf-8")
    legacy_path.write_text(markdown, encoding="utf-8")
    return {"json_path": str(json_path), "markdown_path": str(md_path), "legacy_path": str(legacy_path)}


def render_markdown(handoff):
    parts = [
        "# Implementation handoff",
        "",
        "Route: " + handoff.get("route", "manual_test"),
        "",
        "## Summary",
    ]
    parts.extend(bullets(handoff.get("summary", [])))
    parts.extend(["", "## Verified"])
    parts.extend(bullets(handoff.get("verified", [])))
    parts.extend(["", "## Needs human testing"])
    parts.extend(bullets(handoff.get("needs_human_testing", [])))
    parts.extend(["", "## Known limitations"])
    parts.extend(bullets(handoff.get("known_limitations", [])))
    parts.extend(["", "## Next action", "", handoff.get("next_action", "")])
    return "\n".join(parts).rstrip() + "\n"


def bullets(items):
    if not items:
        return ["- None recorded."]
    return ["- " + str(item) for item in items]


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
