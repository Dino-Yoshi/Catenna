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

    def test_required_sections_need_body_content(self):
        text = "# Stage 5 - Implementation report\n\n" + "\n\n".join(
            "## " + section for section in CONTRACTS["05"].sections
        ) + "\n"

        result = validate_text(text, CONTRACTS["05"])

        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "section has no body content: Summary of changes")

    def test_required_section_body_can_be_terse(self):
        text = valid_artifact("05").replace("Mock content for Deviations from brief.", "None.")

        result = validate_text(text, CONTRACTS["05"])

        self.assertTrue(result["valid"], result)

    def test_required_section_whitespace_only_body_is_rejected(self):
        text = valid_artifact("05").replace("Mock content for Files changed.", "   \n\t")

        result = validate_text(text, CONTRACTS["05"])

        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "section has no body content: Files changed")

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

    def test_yaml_gate_rejects_ready_with_blocking_issues(self):
        gate_body = (
            "ready_for_implementation: true\n"
            "blocking_issues:\n"
            "  - blocker\n"
            "nonblocking_issues: []\n"
            "required_revision_targets: []"
        )

        for stage_key in ("03", "04_gate"):
            result = validate_text(gate_artifact(stage_key, gate_body), CONTRACTS[stage_key])
            self.assertFalse(result["valid"])
            self.assertEqual(result["reason"], "ready_for_implementation is true but blocking_issues is non-empty")

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
        plus = manual_notes("Decision", "+ [X] Reject")

        self.assertTrue(validate_text(star, CONTRACTS["06"])["valid"])
        self.assertTrue(validate_text(plus, CONTRACTS["06"])["valid"])

    def test_stage_6_decision_heading_body_is_required(self):
        result = validate_text(manual_notes("Decision", "  \n\t"), CONTRACTS["06"])
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "section has no body content: Decision")

    def test_stage_6_rejects_duplicate_checked_outcome_lines(self):
        text = manual_notes("Decision", "- [x] Accept\n* [X] Accept\n+ [ ] Reject")
        result = validate_text(text, CONTRACTS["06"])

        self.assertFalse(result["valid"])
        self.assertIn("exactly one checked", result["reason"])

    def test_stage_6_prose_only_notes_require_checkbox_correction(self):
        cases = [
            manual_notes("Decision", "Manual verification passed."),
            manual_notes("Decision", "Not approved."),
            manual_notes("Decision", "Tests did not pass."),
            manual_notes("Decision", "Do not accept this change."),
            manual_notes("Overall manual result", "Approved."),
        ]
        for text in cases:
            result = validate_text(text, CONTRACTS["06"])
            self.assertFalse(result["valid"], text)
            self.assertIn("Decision", result["reason"])
            self.assertIsNone(manual_test_decision(text), text)

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
        self.assertIn("exactly one checked", result["reason"])

    def test_stage_6_rejects_duplicate_decision_sections(self):
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

        result = validate_text(text, CONTRACTS["06"])
        self.assertFalse(result["valid"])
        self.assertIn("duplicate Decision sections", result["reason"])
        self.assertIsNone(manual_test_decision(text))

    def test_stage_6_unchecked_marker_variants_do_not_count_as_prose(self):
        text = manual_notes("Decision", "* [ ] Accept\n+ [ ] Reject\n- [ ] Needs follow-up")
        result = validate_text(text, CONTRACTS["06"])

        self.assertFalse(result["valid"])
        self.assertIn("exactly one checked", result["reason"])

    def test_yaml_gate_parses_multiline_arrays_before_contradiction_check(self):
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
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "ready_for_implementation is true but blocking_issues is non-empty")
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

    def test_yaml_gate_accepts_code_punctuation_in_multiline_list_items(self):
        text = gate_artifact(
            "03",
            "\n".join(
                [
                    "ready_for_implementation: true",
                    "blocking_issues: []",
                    "nonblocking_issues:",
                    "  - foo(bar, baz=1)",
                    "  - compare values[0] with map{key}=value",
                    "required_revision_targets: []",
                ]
            ),
        )

        result = validate_text(text, CONTRACTS["03"])
        self.assertTrue(result["valid"], result)
        gate = parse_gate(text)["gate"]
        self.assertEqual(gate["nonblocking_issues"][0], "foo(bar, baz=1)")
        self.assertEqual(gate["nonblocking_issues"][1], "compare values[0] with map{key}=value")

    def test_yaml_gate_accepts_commas_inside_quoted_inline_array_items(self):
        text = gate_artifact(
            "03",
            "\n".join(
                [
                    "ready_for_implementation: false",
                    'blocking_issues: ["initialize(..., enabled=False, ...) has no defined config source, and conflicts with the existing precedent"]',
                    'nonblocking_issues: ["first item, with a comma", "second item, with another"]',
                    "required_revision_targets: []",
                ]
            ),
        )

        result = validate_text(text, CONTRACTS["03"])
        self.assertTrue(result["valid"], result)
        gate = parse_gate(text)["gate"]
        self.assertEqual(
            gate["blocking_issues"],
            ["initialize(..., enabled=False, ...) has no defined config source, and conflicts with the existing precedent"],
        )
        self.assertEqual(gate["nonblocking_issues"], ["first item, with a comma", "second item, with another"])

    def test_yaml_gate_rejects_inline_array_code_punctuation(self):
        text = gate_artifact(
            "03",
            "\n".join(
                [
                    "ready_for_implementation: true",
                    "blocking_issues: []",
                    "nonblocking_issues: [foo(bar, baz=1)]",
                    "required_revision_targets: []",
                ]
            ),
        )

        result = validate_text(text, CONTRACTS["03"])
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "unsupported gate syntax")

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
        nested_array = gate_artifact(
            "03",
            "ready_for_implementation: true\nblocking_issues:\n  - [B1, B2]\nnonblocking_issues: []\nrequired_revision_targets: []",
        )

        self.assertFalse(validate_text(orphan, CONTRACTS["03"])["valid"])
        self.assertFalse(validate_text(object_like, CONTRACTS["03"])["valid"])
        self.assertFalse(validate_text(nested, CONTRACTS["03"])["valid"])
        self.assertFalse(validate_text(nested_array, CONTRACTS["03"])["valid"])


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

    def test_prose_never_determines_a_decision(self):
        cases = [
            "Approved.",
            "Rejected.",
            "Needs follow-up.",
            "Not approved.",
            "Tests did not pass.",
            "Do not accept this change.",
            "Accepted the direction but rejected the implementation.",
        ]
        for body in cases:
            text = self.section(body)
            result = validate_text(text, CONTRACTS["06"])
            self.assertFalse(result["valid"], body)
            self.assertIn("narrative text is not authoritative", result["reason"])
            self.assertIsNone(manual_test_decision(text), body)

    def test_legacy_overall_manual_result_requires_decision_section(self):
        text = "# Stage 6 - Manual test notes\n\n## Overall manual result\n\n- [x] Accept\n"
        result = validate_text(text, CONTRACTS["06"])
        self.assertFalse(result["valid"])
        self.assertIn("missing sections: Decision", result["reason"])
        self.assertIsNone(manual_test_decision(text))

    def test_no_determinable_outcome_returns_none(self):
        text = self.section("Nothing conclusive was written here.")
        self.assertIsNone(manual_test_decision(text))


if __name__ == "__main__":
    unittest.main()
