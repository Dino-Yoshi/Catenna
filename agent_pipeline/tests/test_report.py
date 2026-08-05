from __future__ import print_function

import json
import tempfile
import unittest
from pathlib import Path

from agent_pipeline import report
from agent_pipeline.state import new_state, orchestrator_dir


STAGE_02_BODY = "\n".join(
    [
        "# Stage 2 - Technical specification",
        "",
        "## Summary",
        "Add a widget.",
        "",
        "## Source request",
        "n/a",
        "## Must-have requirements",
        "n/a",
        "## Nice-to-have requirements",
        "n/a",
        "## Non-goals",
        "n/a",
        "## Affected systems",
        "n/a",
        "## Proposed implementation shape",
        "n/a",
        "## Data/config/API changes",
        "n/a",
        "## Compatibility constraints",
        "n/a",
        "## Risks and edge cases",
        "n/a",
        "## Acceptance criteria",
        "n/a",
        "## Verification plan",
        "n/a",
        "## Open questions",
        "n/a",
        "",
    ]
)

DECISION_BODY = "\n".join(
    [
        "# Stage 8 - Final decision",
        "",
        "## Decision",
        "- [x] Accept",
        "- [ ] Reject",
        "- [ ] Needs follow-up",
        "",
        "## Reason",
        "Everything checked out and verification passed cleanly.",
        "",
        "## Follow-up task, if needed",
        "None.",
        "",
    ]
)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.task_dir = Path(self.tmp.name) / "task"
        self.task_dir.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_task_renders_placeholders_without_crashing(self):
        state = new_state("empty-task")
        result = report.generate_report(self.task_dir, "empty-task", state)
        markdown = report.render_markdown(result)

        self.assertIn("Not yet decided", markdown)
        self.assertIn("No verification report recorded", markdown)
        self.assertIn("No usage ledger entries recorded", markdown)
        self.assertIn("No reasoning traces captured", markdown)
        self.assertIn("No fallback/retry events recorded", markdown)

    def test_populated_task_report_covers_all_sections(self):
        (self.task_dir / "02_technical_spec.md").write_text(STAGE_02_BODY, encoding="utf-8")
        (self.task_dir / "08_decision.md").write_text(DECISION_BODY, encoding="utf-8")
        (self.task_dir / "05_verification_report.json").write_text(
            json.dumps({
                "overall_status": "passed",
                "checks": [{"name": "unit_tests", "status": "passed"}],
                "test_coverage_delta_signal": {"status": "ok"},
            }),
            encoding="utf-8",
        )
        runs_dir = orchestrator_dir(self.task_dir) / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / "02-pass-1-attempt-1-codex-run-a.reasoning.md").write_text(
            "# Reasoning trace\n\nfirst I checked the schema, then wrote the spec\n", encoding="utf-8"
        )
        (runs_dir / "02-pass-1-attempt-1-codex-run-a.json").write_text(
            json.dumps({"stage": "02", "agent": "codex", "run_id": "run-a"}), encoding="utf-8"
        )

        state = new_state("populated-task")
        state["state"] = "complete"
        state["current_stage"] = None
        state["completed_stages"] = ["00", "01", "02"]
        state["artifact_status"] = {
            "02_technical_spec.md": {"stage": "02", "status": "valid", "reason": "valid"},
        }
        state["stage_agents"] = {"02": "codex"}
        state["real_stage_runs"] = {"02": [{"duration_seconds": 12.5, "failure_class": None}]}
        state["fallback_events"] = [{"stage": "02", "from": "codex", "to": "claude"}]

        usage_entries = [
            {"task": "populated-task", "agent": "codex", "duration_seconds": 12.5, "failure_class": None, "usage": {"input_tokens": 100, "output_tokens": 20}},
        ]

        result = report.generate_report(self.task_dir, "populated-task", state, usage_entries=usage_entries)
        markdown = report.render_markdown(result)

        self.assertIn("| 02 | valid | codex | 12.5s | - |", markdown)
        self.assertIn("Add a widget.", markdown)
        self.assertIn("Final decision: **accept**", markdown)
        self.assertIn("Everything checked out", markdown)
        self.assertIn("Overall status: **passed**", markdown)
        self.assertIn("unit_tests: passed", markdown)
        self.assertIn("codex: calls=1", markdown)
        self.assertIn("first I checked the schema", markdown)
        self.assertIn("from", markdown)

    def test_missing_decision_and_invalid_decision_file_both_report_not_yet_decided(self):
        state = new_state("no-decision-task")
        result = report.generate_report(self.task_dir, "no-decision-task", state)
        self.assertIsNone(result["decision"])

        (self.task_dir / "08_decision.md").write_text("garbage, not a valid decision doc\n", encoding="utf-8")
        result2 = report.generate_report(self.task_dir, "no-decision-task", state)
        self.assertIsNone(result2["decision"])

    def test_report_files_are_written_under_orchestrator_dir(self):
        state = new_state("write-task")
        result = report.generate_report(self.task_dir, "write-task", state)
        json_path = Path(result["report_paths"]["json_path"])
        md_path = Path(result["report_paths"]["md_path"])
        self.assertEqual(json_path.parent, orchestrator_dir(self.task_dir))
        self.assertTrue(json_path.exists())
        self.assertTrue(md_path.exists())
        self.assertEqual(json.loads(json_path.read_text(encoding="utf-8"))["task"], "write-task")


if __name__ == "__main__":
    unittest.main()
