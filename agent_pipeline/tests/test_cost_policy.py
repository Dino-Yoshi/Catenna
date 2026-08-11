from __future__ import print_function

import unittest

from agent_pipeline import config
from agent_pipeline import cost_policy


class CostPolicyTests(unittest.TestCase):
    def enabled_config(self):
        cfg = config.deep_copy(config.DEFAULT_CONFIG)
        cfg["cost_control"]["enabled"] = True
        cfg["cost_control"]["min_samples"] = 2
        cfg["cost_control"]["max_retry_rate"] = 0.5
        cfg["cost_control"]["downgrade_candidates"] = {
            "claude": {"model": "claude-haiku-4-5", "effort": "low"},
            "codex": {"model": "gpt-mini"},
            "agy": {"effort": "low"},
        }
        return cfg

    def entry(self, stage, agent, failure_class=None, attempt_number=1, pass_number=1, model=None):
        entry = {
            "stage": stage,
            "agent": agent,
            "failure_class": failure_class,
            "attempt_number": attempt_number,
            "pass_number": pass_number,
        }
        if model is not None:
            entry["model"] = model
        return entry

    def test_disabled_returns_empty(self):
        cfg = config.deep_copy(config.DEFAULT_CONFIG)

        self.assertEqual(cost_policy.compute_stage_overrides(cfg, [self.entry("02", "claude")]), {})

    def test_clean_agent_specific_history_authorizes_matching_candidate_only(self):
        cfg = self.enabled_config()
        entries = [
            self.entry("04_gate", "claude"),
            self.entry("04_gate", "claude"),
            self.entry("04_gate", "codex"),
            self.entry("04_gate", "codex", failure_class="timeout"),
        ]

        self.assertEqual(
            cost_policy.compute_stage_overrides(cfg, entries)["04_gate"]["claude"],
            {"model": "claude-haiku-4-5", "effort": "low"},
        )
        self.assertNotIn("codex", cost_policy.compute_stage_overrides(cfg, entries).get("04_gate", {}))

    def test_stage_only_history_does_not_authorize_different_agent(self):
        cfg = self.enabled_config()
        cfg["cost_control"]["downgrade_candidates"] = {"agy": {"effort": "low"}}
        entries = [self.entry("03", "claude"), self.entry("03", "claude")]

        self.assertEqual(cost_policy.compute_stage_overrides(cfg, entries), {})

    def test_thin_history_and_any_failure_are_ineligible(self):
        cfg = self.enabled_config()

        self.assertNotIn("02", cost_policy.compute_stage_overrides(cfg, [self.entry("02", "claude")]))
        self.assertNotIn(
            "02",
            cost_policy.compute_stage_overrides(
                cfg,
                [self.entry("02", "claude"), self.entry("02", "claude", failure_class="timeout")],
            ),
        )

    def test_retry_threshold_is_strict_and_missing_attempt_counts_as_non_retry(self):
        cfg = self.enabled_config()
        entries = [
            {"stage": "02", "agent": "claude", "failure_class": None},
            self.entry("02", "claude", attempt_number=2),
        ]

        self.assertNotIn("02", cost_policy.compute_stage_overrides(cfg, entries))

        cfg["cost_control"]["max_retry_rate"] = 0.51
        self.assertIn("02", cost_policy.compute_stage_overrides(cfg, entries))

    def test_allowed_stage_intersection_excludes_stage5_and_overseer(self):
        cfg = self.enabled_config()
        cfg["cost_control"]["eligible_stages"] = ["05", "overseer", "07"]
        entries = [
            self.entry("05", "claude"),
            self.entry("05", "claude"),
            self.entry("overseer", "claude"),
            self.entry("overseer", "claude"),
            self.entry("07", "claude"),
            self.entry("07", "claude"),
        ]

        self.assertEqual(list(cost_policy.compute_stage_overrides(cfg, entries).keys()), ["07"])

    def test_static_role_override_blocks_dynamic_override(self):
        cfg = self.enabled_config()
        cfg["roles"]["02"]["model_override"] = "fixed"
        entries = [self.entry("02", "claude"), self.entry("02", "claude")]

        self.assertEqual(cost_policy.compute_stage_overrides(cfg, entries), {})

    def test_null_empty_and_partial_candidates(self):
        cfg = self.enabled_config()
        cfg["cost_control"]["downgrade_candidates"] = {
            "claude": None,
            "codex": {},
            "agy": {"effort": "low"},
        }
        entries = [
            self.entry("03", "claude"),
            self.entry("03", "claude"),
            self.entry("03", "codex"),
            self.entry("03", "codex"),
            self.entry("03", "agy"),
            self.entry("03", "agy"),
        ]

        self.assertEqual(cost_policy.compute_stage_overrides(cfg, entries), {"03": {"agy": {"effort": "low"}}})

    def test_cold_start_candidate_history_below_min_samples_still_applies_override(self):
        cfg = self.enabled_config()
        entries = [
            self.entry("02", "claude", model="claude-sonnet-4-5"),
            self.entry("02", "claude", model="claude-sonnet-4-5"),
            self.entry("02", "claude", failure_class="timeout", model="claude-haiku-4-5"),
        ]

        self.assertEqual(
            cost_policy.compute_stage_overrides(cfg, entries)["02"]["claude"],
            {"model": "claude-haiku-4-5", "effort": "low"},
        )

    def test_sufficient_clean_candidate_history_still_applies_override(self):
        cfg = self.enabled_config()
        entries = [
            self.entry("02", "claude", model="claude-sonnet-4-5"),
            self.entry("02", "claude", model="claude-sonnet-4-5"),
            self.entry("02", "claude", model="claude-haiku-4-5"),
            self.entry("02", "claude", model="claude-haiku-4-5"),
        ]

        self.assertEqual(
            cost_policy.compute_stage_overrides(cfg, entries)["02"]["claude"],
            {"model": "claude-haiku-4-5", "effort": "low"},
        )

    def test_sufficient_candidate_retry_rate_at_threshold_withholds_override(self):
        cfg = self.enabled_config()
        entries = [
            self.entry("02", "claude", model="claude-sonnet-4-5"),
            self.entry("02", "claude", model="claude-sonnet-4-5"),
            self.entry("02", "claude", model="claude-haiku-4-5"),
            self.entry("02", "claude", attempt_number=2, model="claude-haiku-4-5"),
        ]

        self.assertNotIn("02", cost_policy.compute_stage_overrides(cfg, entries))

    def test_sufficient_candidate_history_with_non_null_failure_withholds_override(self):
        cfg = self.enabled_config()
        entries = [
            self.entry("02", "claude", model="claude-sonnet-4-5"),
            self.entry("02", "claude", model="claude-sonnet-4-5"),
            self.entry("02", "claude", failure_class="", model="claude-haiku-4-5"),
            self.entry("02", "claude", model="claude-haiku-4-5"),
        ]

        self.assertNotIn("02", cost_policy.compute_stage_overrides(cfg, entries))

    def test_reconfigured_candidate_model_ignores_old_model_history(self):
        cfg = self.enabled_config()
        cfg["cost_control"]["downgrade_candidates"]["claude"]["model"] = "claude-small-new"
        entries = [
            self.entry("02", "claude", model="claude-sonnet-4-5"),
            self.entry("02", "claude", model="claude-sonnet-4-5"),
            self.entry("03", "claude", failure_class="timeout", model="claude-haiku-4-5"),
            self.entry("02", "codex", attempt_number=2, model="claude-haiku-4-5"),
        ]

        self.assertEqual(
            cost_policy.compute_stage_overrides(cfg, entries)["02"]["claude"],
            {"model": "claude-small-new", "effort": "low"},
        )

    def test_baseline_excludes_current_candidate_model_history(self):
        cfg = self.enabled_config()
        entries = [
            self.entry("02", "claude", model="claude-haiku-4-5"),
            self.entry("02", "claude", model="claude-haiku-4-5"),
        ]

        self.assertEqual(cost_policy.compute_stage_overrides(cfg, entries), {})

    def test_effort_only_candidate_remains_compatible_with_model_history(self):
        cfg = self.enabled_config()
        cfg["cost_control"]["downgrade_candidates"] = {"agy": {"effort": "low"}}
        entries = [
            self.entry("03", "agy", model="agy-baseline"),
            self.entry("03", "agy", model="agy-other-baseline"),
        ]

        self.assertEqual(cost_policy.compute_stage_overrides(cfg, entries), {"03": {"agy": {"effort": "low"}}})


if __name__ == "__main__":
    unittest.main()
