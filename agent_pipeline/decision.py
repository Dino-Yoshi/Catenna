"""Stage 8 decision synthesis helpers."""

from __future__ import print_function

from .artifacts import CONTRACTS, manual_test_decision
from .failures import EXIT_BLOCKED, EXIT_SUCCESS
from .runner import atomic_finalize
from .state import append_log


_DECISION_RANK = {"accept": 0, "needs_followup": 1, "reject": 2}


def worse_decision(a, b):
    return a if _DECISION_RANK.get(a, 2) >= _DECISION_RANK.get(b, 2) else b


def last_nonempty_line(text):
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return None


def render_stage08_decision(final_decision, stage06_outcome, stage07_verdict):
    boxes = []
    for key, label in (("accept", "Accept"), ("reject", "Reject"), ("needs_followup", "Needs follow-up")):
        mark = "x" if key == final_decision else " "
        boxes.append("- [%s] %s" % (mark, label))
    follow_up = (
        "None -- accepted automatically."
        if final_decision == "accept"
        else "See Stage 7 diff review's \"Required fixes\" section before merging."
    )
    lines = [
        CONTRACTS["08"].heading,
        "",
        "## Decision",
        "",
    ]
    lines.extend(boxes)
    lines.extend([
        "",
        "## Reason",
        "",
        "Derived automatically from the Stage 6 manual test result (%s) and the Stage 7" % stage06_outcome,
        "diff review verdict (%s). See 06_manual_test_notes.md and 07_diff_review.md" % stage07_verdict,
        "for full detail.",
        "",
        "## Follow-up task, if needed",
        "",
        follow_up,
    ])
    return "\n".join(lines).rstrip() + "\n"


def ensure_stage08_decision(task_dir, state, block_transition):
    if "08" not in state.get("completed_stages", []):
        stage06_text = (task_dir / CONTRACTS["06"].filename).read_text(encoding="utf-8")
        stage07_text = (task_dir / CONTRACTS["07"].filename).read_text(encoding="utf-8")
        stage06_outcome = manual_test_decision(stage06_text) or "needs_followup"
        stage07_verdict = last_nonempty_line(stage07_text) or "needs_followup"
        final_decision = worse_decision(stage06_outcome, stage07_verdict)
        content = render_stage08_decision(final_decision, stage06_outcome, stage07_verdict)
        result = atomic_finalize(task_dir, "08", content)
        if not result["finalized"]:
            block_transition(task_dir, state, "08", result["validation"]["reason"], result["validation"].get("failure_class"), completed_through="07")
            return EXIT_BLOCKED, None
        append_log(task_dir, {
            "event": "stage8_decision_synthesized",
            "stage": "08",
            "decision": final_decision,
            "stage06_outcome": stage06_outcome,
            "stage07_verdict": stage07_verdict,
            "run_id": state.get("run_id"),
        })
    stage08_text = (task_dir / CONTRACTS["08"].filename).read_text(encoding="utf-8")
    final_decision = manual_test_decision(stage08_text) or "needs_followup"
    return EXIT_SUCCESS, final_decision
