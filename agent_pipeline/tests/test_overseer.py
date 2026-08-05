from __future__ import print_function

import json
import unittest

from agent_pipeline.overseer import fallback_handoff, parse_overseer_candidate, upgrade_to_auto_verified


def valid_payload(**overrides):
    payload = {
        "route": "manual_test",
        "summary": ["did the thing"],
        "verified": ["unit tests pass"],
        "needs_human_testing": ["in-game check"],
        "known_limitations": [],
        "next_action": "Run Stage 6.",
    }
    payload.update(overrides)
    return payload


class ParseOverseerCandidateTests(unittest.TestCase):
    def test_accepts_dict_input_directly(self):
        result = parse_overseer_candidate(valid_payload())
        self.assertEqual(result["route"], "manual_test")

    def test_accepts_json_string_input(self):
        result = parse_overseer_candidate(json.dumps(valid_payload()))
        self.assertEqual(result["next_action"], "Run Stage 6.")

    def test_rejects_invalid_json_string(self):
        with self.assertRaises(ValueError):
            parse_overseer_candidate("not json at all")

    def test_rejects_unknown_route(self):
        with self.assertRaises(ValueError):
            parse_overseer_candidate(valid_payload(route="not_a_real_route"))

    def test_accepts_each_allowed_route(self):
        for route in ("manual_test", "blocked", "administrator_action", "auto_verified"):
            result = parse_overseer_candidate(valid_payload(route=route))
            self.assertEqual(result["route"], route)

    def test_rejects_non_list_summary(self):
        with self.assertRaises(ValueError):
            parse_overseer_candidate(valid_payload(summary="did the thing"))

    def test_rejects_missing_list_field(self):
        payload = valid_payload()
        del payload["known_limitations"]
        with self.assertRaises(ValueError):
            parse_overseer_candidate(payload)

    def test_rejects_missing_next_action(self):
        with self.assertRaises(ValueError):
            parse_overseer_candidate(valid_payload(next_action=""))

    def test_rejects_non_string_next_action(self):
        with self.assertRaises(ValueError):
            parse_overseer_candidate(valid_payload(next_action=["not a string"]))


class FallbackHandoffTests(unittest.TestCase):
    def test_route_is_manual_test(self):
        handoff = fallback_handoff({"changed_files": []}, "agent unavailable")
        self.assertEqual(handoff["route"], "manual_test")
        self.assertTrue(handoff["fallback"])

    def test_reason_is_recorded_in_known_limitations(self):
        handoff = fallback_handoff({"changed_files": []}, "agent unavailable")
        self.assertTrue(any("agent unavailable" in item for item in handoff["known_limitations"]))

    def test_changed_files_are_pulled_from_manifest(self):
        manifest = {"changed_files": [{"path": "src/A.java"}, {"path": "src/B.java"}]}
        handoff = fallback_handoff(manifest, "reason")
        self.assertEqual(handoff["changed_files"], ["src/A.java", "src/B.java"])

    def test_missing_changed_files_defaults_to_empty_list(self):
        handoff = fallback_handoff({}, "reason")
        self.assertEqual(handoff["changed_files"], [])

    def test_result_parses_as_a_valid_overseer_candidate(self):
        # fallback_handoff's own output must satisfy parse_overseer_candidate's
        # contract, since write_handoff_files/downstream code treats both the
        # same way.
        handoff = fallback_handoff({"changed_files": []}, "reason")
        parsed = parse_overseer_candidate(handoff)
        self.assertEqual(parsed["route"], "manual_test")

    def test_no_verification_report_keeps_pessimistic_verified_bullet(self):
        handoff = fallback_handoff({"changed_files": []}, "reason")
        self.assertEqual(handoff["verified"], ["No automatic verification was marked passed by the controller."])

    def test_verification_report_populates_verified_with_real_check_statuses(self):
        report = {
            "checks": [{"name": "unit_tests", "status": "passed"}, {"name": "gradle_compileJava", "status": "failed"}],
            "test_coverage_delta_signal": {"status": "ok"},
        }
        handoff = fallback_handoff({"changed_files": []}, "reason", verification_report=report)
        self.assertEqual(
            handoff["verified"],
            ["unit_tests: passed", "gradle_compileJava: failed", "test_coverage_delta_signal: ok"],
        )


class UpgradeToAutoVerifiedTests(unittest.TestCase):
    def report(self, **overrides):
        base = {
            "overall_status": "passed",
            "checks": [{"name": "unit_tests", "status": "passed"}, {"name": "gradle_compileJava", "status": "passed"}],
            "test_coverage_delta_signal": {"status": "ok"},
        }
        base.update(overrides)
        return base

    def test_forces_route_to_auto_verified(self):
        handoff = valid_payload(route="manual_test")
        upgraded = upgrade_to_auto_verified(handoff, self.report())
        self.assertEqual(upgraded["route"], "auto_verified")

    def test_populates_verified_from_report(self):
        handoff = valid_payload(route="manual_test")
        upgraded = upgrade_to_auto_verified(handoff, self.report())
        self.assertEqual(upgraded["verified"], ["unit_tests: passed", "gradle_compileJava: passed", "test_coverage_delta_signal: ok"])

    def test_appends_no_human_testing_limitation(self):
        handoff = valid_payload(route="manual_test", known_limitations=["pre-existing note"])
        upgraded = upgrade_to_auto_verified(handoff, self.report())
        self.assertIn("pre-existing note", upgraded["known_limitations"])
        self.assertTrue(any("no human played the mod in-game" in item for item in upgraded["known_limitations"]))

    def test_never_upgrades_a_blocked_route(self):
        handoff = valid_payload(route="blocked")
        upgraded = upgrade_to_auto_verified(handoff, self.report())
        self.assertEqual(upgraded["route"], "blocked")
        self.assertEqual(upgraded, handoff)

    def test_never_upgrades_an_administrator_action_route(self):
        handoff = valid_payload(route="administrator_action")
        upgraded = upgrade_to_auto_verified(handoff, self.report())
        self.assertEqual(upgraded["route"], "administrator_action")
        self.assertEqual(upgraded, handoff)

    def test_result_parses_as_a_valid_overseer_candidate(self):
        handoff = valid_payload(route="manual_test")
        upgraded = upgrade_to_auto_verified(handoff, self.report())
        parsed = parse_overseer_candidate(upgraded)
        self.assertEqual(parsed["route"], "auto_verified")


if __name__ == "__main__":
    unittest.main()
