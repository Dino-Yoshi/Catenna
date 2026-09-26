from __future__ import print_function

import tempfile
import unittest
from pathlib import Path

from agent_pipeline import controller
from agent_pipeline.artifacts import CONTRACTS, manual_test_decision, validate_text
from agent_pipeline.decision import ensure_stage08_decision
from agent_pipeline.failures import EXIT_BLOCKED, EXIT_SUCCESS
from agent_pipeline.mock_agent import valid_artifact


def stage06(choice, narrative="Manual evidence recorded."):
    lines = [
        CONTRACTS["06"].heading,
        "",
        "## Decision",
        "",
        "- [%s] Accept" % ("x" if choice == "accept" else " "),
        "- [%s] Reject" % ("x" if choice == "reject" else " "),
        "- [%s] Needs follow-up" % ("x" if choice == "needs_followup" else " "),
        "",
        "## Notes",
        "",
        narrative,
    ]
    return "\n".join(lines) + "\n"


def stage07(verdict):
    text = valid_artifact("07")
    lines = text.rstrip().splitlines()
    lines[-1] = verdict
    return "\n".join(lines) + "\n"


class Stage08DecisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.task_dir = Path(self.tmp.name)
        self.state = {"completed_stages": ["00", "01", "02", "03", "04", "04_gate", "05", "06", "07"], "run_id": "test-run"}
        self.blocks = []

    def tearDown(self):
        self.tmp.cleanup()

    def block_transition(self, *args, **kwargs):
        self.blocks.append((args, kwargs))

    def write_inputs(self, stage06_text, stage07_verdict):
        (self.task_dir / CONTRACTS["06"].filename).write_text(stage06_text, encoding="utf-8")
        (self.task_dir / CONTRACTS["07"].filename).write_text(stage07(stage07_verdict), encoding="utf-8")

    def test_each_valid_stage6_checkbox_is_consumed(self):
        for stage06_choice in ("accept", "reject", "needs_followup"):
            with self.subTest(stage06_choice=stage06_choice):
                self.state["completed_stages"] = self.state["completed_stages"][:9]
                decision_path = self.task_dir / CONTRACTS["08"].filename
                if decision_path.exists():
                    decision_path.unlink()
                self.write_inputs(stage06(stage06_choice), "accept")

                code, decision = ensure_stage08_decision(self.task_dir, self.state, self.block_transition)

                self.assertEqual(code, EXIT_SUCCESS)
                self.assertEqual(decision, stage06_choice)
                self.assertTrue(validate_text(decision_path.read_text(encoding="utf-8"), CONTRACTS["08"])["valid"])

    def test_invalid_stage6_prose_cannot_reach_stage8(self):
        self.write_inputs(
            CONTRACTS["06"].heading + "\n\n## Decision\n\nDo not accept this change.\n",
            "accept",
        )

        code, decision = ensure_stage08_decision(self.task_dir, self.state, self.block_transition)

        self.assertEqual(code, EXIT_BLOCKED)
        self.assertIsNone(decision)
        self.assertFalse((self.task_dir / CONTRACTS["08"].filename).exists())
        self.assertIn("exactly one checked", self.blocks[0][0][3])

    def test_invalid_stage7_evidence_cannot_reach_stage8(self):
        self.write_inputs(stage06("accept"), "not an allowed verdict")

        code, decision = ensure_stage08_decision(self.task_dir, self.state, self.block_transition)

        self.assertEqual(code, EXIT_BLOCKED)
        self.assertIsNone(decision)
        self.assertFalse((self.task_dir / CONTRACTS["08"].filename).exists())
        self.assertIn("Stage 7 review is invalid", self.blocks[0][0][3])

    def test_reject_then_needs_followup_then_accept_precedence(self):
        cases = [
            ("accept", "accept", "accept"),
            ("accept", "needs_followup", "needs_followup"),
            ("needs_followup", "accept", "needs_followup"),
            ("accept", "reject", "reject"),
            ("reject", "accept", "reject"),
        ]
        for stage06_choice, stage07_verdict, expected in cases:
            with self.subTest(stage06=stage06_choice, stage07=stage07_verdict):
                self.state["completed_stages"] = self.state["completed_stages"][:9]
                decision_path = self.task_dir / CONTRACTS["08"].filename
                if decision_path.exists():
                    decision_path.unlink()
                self.write_inputs(stage06(stage06_choice), stage07_verdict)

                code, decision = ensure_stage08_decision(self.task_dir, self.state, self.block_transition)

                self.assertEqual(code, EXIT_SUCCESS)
                self.assertEqual(decision, expected)

    def test_automatic_acceptance_checkbox_remains_valid(self):
        automatic = controller.render_auto_stage06_notes({
            "checks": [{"name": "driven_project_tests", "status": "passed"}],
            "test_coverage_delta_signal": {"status": "ok"},
        })
        self.assertTrue(validate_text(automatic, CONTRACTS["06"])["valid"])
        self.assertEqual(manual_test_decision(automatic), "accept")

        self.write_inputs(automatic, "accept")
        code, decision = ensure_stage08_decision(self.task_dir, self.state, self.block_transition)

        self.assertEqual(code, EXIT_SUCCESS)
        self.assertEqual(decision, "accept")


if __name__ == "__main__":
    unittest.main()
