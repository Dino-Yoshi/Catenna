from __future__ import print_function

import unittest

from agent_pipeline.artifacts import CONTRACTS, manual_test_decision, parse_gate, validate_text
from agent_pipeline.mock_agent import gate_artifact, valid_artifact


def manual_notes(section, body):
    return "# Stage 6 - Manual test notes\n\n## %s\n\n%s\n" % (section, body)


class ArtifactValidationTests(unittest.TestCase):
    def test_heading_must_be_first_line(self):
        text = "Introductory commentary.\n\n" + valid_artifact("05")
        result = validate_text(text, CONTRACTS["05"])
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "leading commentary before required heading")

    def test_missing_required_sections_are_rejected(self):
        result = validate_text("# Stage 5 - Implementation report\n\n## Summary of changes\n\nDone.\n", CONTRACTS["05"])
        self.assertFalse(result["valid"])
        self.assertIn("Files changed", result["reason"])

    def test_yaml_gate_validates_required_keys_and_types(self):
        self.assertTrue(validate_text(valid_artifact("03"), CONTRACTS["03"])["valid"])

        malformed = validate_text(gate_artifact("03", "ready_for_implementation true"), CONTRACTS["03"])
        self.assertFalse(malformed["valid"])
        self.assertEqual(malformed["reason"], "malformed gate syntax")

        missing = validate_text(
            gate_artifact(
                "03",
                "ready_for_implementation: true\nblocking_issues: []\nnonblocking_issues: []",
            ),
            CONTRACTS["03"],
        )
        self.assertFalse(missing["valid"])
        self.assertEqual(missing["reason"], "missing gate key: required_revision_targets")

    def test_stage_6_requires_explicit_manual_outcome(self):
        heading_only = manual_notes("Decision", "")
        unchecked = manual_notes("Decision", "- [ ] Accept\n- [ ] Reject\n- [ ] Needs follow-up")
        generic = manual_notes("Decision", "Mock content for Decision.")
        multiple = manual_notes("Decision", "- [x] Accept\n- [X] Reject\n- [ ] Needs follow-up")

        for text in (heading_only, unchecked, generic, multiple):
            self.assertFalse(validate_text(text, CONTRACTS["06"])["valid"])

    def test_stage_6_accepts_one_checked_decision(self):
        text = manual_notes("Decision", "- [ ] Accept\n- [X] Reject\n- [ ] Needs follow-up")
        self.assertTrue(validate_text(text, CONTRACTS["06"])["valid"])

    def test_stage_6_accepts_standard_task_list_markers(self):
        star = manual_notes("Decision", "- [ ] Accept\n- [ ] Reject\n* [x] Needs follow-up")
        plus = manual_notes("Overall manual result", "+ [X] Reject")

        self.assertTrue(validate_text(star, CONTRACTS["06"])["valid"])
        self.assertTrue(validate_text(plus, CONTRACTS["06"])["valid"])

    def test_stage_6_rejects_duplicate_checked_outcome_lines(self):
        text = manual_notes("Decision", "- [x] Accept\n* [X] Accept\n+ [ ] Reject")
        result = validate_text(text, CONTRACTS["06"])

        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "exactly one manual decision checkbox must be checked")

    def test_stage_6_accepts_clear_outcome_prose(self):
        decision = manual_notes("Decision", "Manual verification passed.")
        overall = manual_notes("Overall manual result", "Blocked pending a clean review environment.")

        self.assertTrue(validate_text(decision, CONTRACTS["06"])["valid"])
        self.assertTrue(validate_text(overall, CONTRACTS["06"])["valid"])

    def test_stage_6_checkbox_parsing_is_scoped_to_result_section(self):
        text = (
            "# Stage 6 - Manual test notes\n\n"
            "## Setup checklist\n\n"
            "- [x] Accept\n\n"
            "## Decision\n\n"
            "Mock content for Decision.\n"
        )
        result = validate_text(text, CONTRACTS["06"])
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "manual test notes must state an explicit outcome")

    def test_stage_6_uses_last_result_section(self):
        text = (
            "# Stage 6 - Manual test notes\n\n"
            "## Decision\n\n"
            "- [ ] Accept\n"
            "- [ ] Reject\n"
            "- [ ] Needs follow-up\n\n"
            "## Notes\n\n"
            "- [x] Accept\n\n"
            "## Decision\n\n"
            "* [x] Needs follow-up\n"
        )

        self.assertTrue(validate_text(text, CONTRACTS["06"])["valid"])

    def test_stage_6_unchecked_marker_variants_do_not_count_as_prose(self):
        text = manual_notes("Decision", "* [ ] Accept\n+ [ ] Reject\n- [ ] Needs follow-up")
        result = validate_text(text, CONTRACTS["06"])

        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "manual test notes must state an explicit outcome")

    def test_yaml_gate_accepts_multiline_arrays(self):
        text = gate_artifact(
            "03",
            "\n".join(
                [
                    "ready_for_implementation: true",
                    "blocking_issues:",
                    '  - "B1: quoted text, with comma"',
                    "  - bare_identifier",
                    "nonblocking_issues:",
                    "required_revision_targets: []",
                ]
            ),
        )

        result = validate_text(text, CONTRACTS["03"])
        self.assertTrue(result["valid"])
        gate = parse_gate(text)["gate"]
        self.assertEqual(gate["blocking_issues"], ["B1: quoted text, with comma", "bare_identifier"])
        self.assertEqual(gate["nonblocking_issues"], [])

    def test_yaml_gate_accepts_escaped_quotes_in_list_items(self):
        text = gate_artifact(
            "03",
            "\n".join(
                [
                    "ready_for_implementation: true",
                    "blocking_issues: []",
                    "nonblocking_issues:",
                    '  - "the value is \\"05\\" here, at file.py:12-19; note it."',
                    "required_revision_targets: []",
                ]
            ),
        )

        result = validate_text(text, CONTRACTS["03"])
        self.assertTrue(result["valid"], result)
        gate = parse_gate(text)["gate"]
        self.assertEqual(gate["nonblocking_issues"], ['the value is "05" here, at file.py:12-19; note it.'])

    def test_yaml_gate_rejects_orphan_and_nested_list_syntax(self):
        orphan = gate_artifact(
            "03",
            "ready_for_implementation: true\n- B1\nblocking_issues: []\nnonblocking_issues: []\nrequired_revision_targets: []",
        )
        object_like = gate_artifact(
            "03",
            "ready_for_implementation: true\nblocking_issues:\n  - key: value\nnonblocking_issues: []\nrequired_revision_targets: []",
        )
        nested = gate_artifact(
            "03",
            "ready_for_implementation: true\nblocking_issues:\n  - B1\n    detail\nnonblocking_issues: []\nrequired_revision_targets: []",
        )

        self.assertFalse(validate_text(orphan, CONTRACTS["03"])["valid"])
        self.assertFalse(validate_text(object_like, CONTRACTS["03"])["valid"])
        self.assertFalse(validate_text(nested, CONTRACTS["03"])["valid"])


class ManualTestDecisionTests(unittest.TestCase):
    def section(self, body):
        return "# Stage 6 - Manual test notes\n\n## Decision\n\n" + body + "\n"

    def test_checked_accept_box(self):
        text = self.section("- [x] Accept\n- [ ] Reject\n- [ ] Needs follow-up")
        self.assertEqual(manual_test_decision(text), "accept")

    def test_checked_reject_box(self):
        text = self.section("- [ ] Accept\n- [x] Reject\n- [ ] Needs follow-up")
        self.assertEqual(manual_test_decision(text), "reject")

    def test_checked_needs_followup_box(self):
        text = self.section("- [ ] Accept\n- [ ] Reject\n- [x] Needs follow-up")
        self.assertEqual(manual_test_decision(text), "needs_followup")

    def test_prose_accept(self):
        text = self.section("Tested in-game; the enchant applies correctly. Approved.")
        self.assertEqual(manual_test_decision(text), "accept")

    def test_prose_reject(self):
        text = self.section("The tooltip crashed the client. Rejected.")
        self.assertEqual(manual_test_decision(text), "reject")

    def test_prose_needs_followup(self):
        text = self.section("Mostly works but needs follow-up on the anvil recipe.")
        self.assertEqual(manual_test_decision(text), "needs_followup")

    def test_prose_reject_wins_over_accept_mentioned_together(self):
        text = self.section("Accepted the overall direction but the build failed in testing, so this is rejected.")
        self.assertEqual(manual_test_decision(text), "reject")

    def test_prose_reject_wins_over_needs_followup_mentioned_together(self):
        text = self.section("Needs follow-up on docs, but the core change is broken and blocked from merging.")
        self.assertEqual(manual_test_decision(text), "reject")

    def test_overall_manual_result_heading_variant(self):
        text = "# Stage 6 - Manual test notes\n\n## Overall manual result\n\n- [x] Accept\n"
        self.assertEqual(manual_test_decision(text), "accept")

    def test_no_determinable_outcome_returns_none(self):
        text = self.section("Nothing conclusive was written here.")
        self.assertIsNone(manual_test_decision(text))


if __name__ == "__main__":
    unittest.main()
