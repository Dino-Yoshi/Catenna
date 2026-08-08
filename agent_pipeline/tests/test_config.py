from __future__ import print_function

import unittest

from agent_pipeline import config


class ReasoningCaptureConfigTests(unittest.TestCase):
    def test_default_config_is_valid(self):
        self.assertTrue(config.validate_config(config.deep_copy(config.DEFAULT_CONFIG)))

    def test_unknown_role_primary_is_rejected(self):
        bad = config.deep_copy(config.DEFAULT_CONFIG)
        bad["roles"]["02"]["primary"] = "missing-agent"
        with self.assertRaises(config.ConfigError) as raised:
            config.validate_config(bad)
        self.assertIn("02", str(raised.exception))
        self.assertIn("missing-agent", str(raised.exception))

    def test_unknown_role_fallback_is_rejected(self):
        bad = config.deep_copy(config.DEFAULT_CONFIG)
        bad["roles"]["03"]["fallbacks"] = ["missing-agent"]
        with self.assertRaises(config.ConfigError) as raised:
            config.validate_config(bad)
        self.assertIn("03", str(raised.exception))
        self.assertIn("missing-agent", str(raised.exception))

    def test_disabled_but_present_fallback_is_valid(self):
        cfg = config.deep_copy(config.DEFAULT_CONFIG)
        cfg["agents"]["disabled-agent"] = {
            "command": "",
            "enabled": False,
            "workspace_write": False,
        }
        cfg["roles"]["04"]["fallbacks"] = ["disabled-agent"]
        self.assertTrue(config.validate_config(cfg))

    def test_default_config_enables_reasoning_capture(self):
        self.assertTrue(config.DEFAULT_CONFIG["reasoning_capture"]["enabled"])

    def test_non_boolean_reasoning_capture_enabled_is_rejected(self):
        bad = config.deep_copy(config.DEFAULT_CONFIG)
        bad["reasoning_capture"]["enabled"] = "yes"
        with self.assertRaises(config.ConfigError):
            config.validate_config(bad)

    def test_missing_reasoning_capture_section_is_rejected(self):
        bad = config.deep_copy(config.DEFAULT_CONFIG)
        del bad["reasoning_capture"]
        with self.assertRaises(config.ConfigError):
            config.validate_config(bad)

    def test_requested_top_level_booleans_must_be_real_booleans(self):
        for field in ("enable_auto_verified", "allow_degraded_same_agent_review"):
            bad = config.deep_copy(config.DEFAULT_CONFIG)
            bad[field] = "true"
            with self.assertRaises(config.ConfigError) as raised:
                config.validate_config(bad)
            self.assertIn(field, str(raised.exception))

    def test_timeout_seconds_must_be_positive_integer_not_bool(self):
        for value in (None, "30", True, 0, -1):
            bad = config.deep_copy(config.DEFAULT_CONFIG)
            if value is None:
                del bad["timeout_seconds"]
            else:
                bad["timeout_seconds"] = value
            with self.assertRaises(config.ConfigError) as raised:
                config.validate_config(bad)
            self.assertIn("timeout_seconds", str(raised.exception))

    def test_turn_budgets_must_be_mapping_with_positive_integer_values(self):
        bad = config.deep_copy(config.DEFAULT_CONFIG)
        bad["turn_budgets"] = []
        with self.assertRaises(config.ConfigError) as raised:
            config.validate_config(bad)
        self.assertIn("turn_budgets", str(raised.exception))

        for value in ("5", False, 0, -1):
            bad = config.deep_copy(config.DEFAULT_CONFIG)
            bad["turn_budgets"]["05"] = value
            with self.assertRaises(config.ConfigError) as raised:
                config.validate_config(bad)
            self.assertIn("turn_budgets.05", str(raised.exception))

    def test_missing_individual_turn_budget_keys_remain_allowed(self):
        cfg = config.deep_copy(config.DEFAULT_CONFIG)
        del cfg["turn_budgets"]["05"]
        self.assertTrue(config.validate_config(cfg))

    def test_default_verification_config_has_no_driven_project_commands(self):
        self.assertEqual(config.DEFAULT_CONFIG["verification"]["driven_project_commands"], [])

    def test_valid_driven_project_commands_are_accepted(self):
        cfg = config.deep_copy(config.DEFAULT_CONFIG)
        cfg["verification"]["driven_project_commands"] = [
            {"name": "pytest.unit", "argv": ["python3", "-m", "pytest"], "timeout_seconds": 30},
            {"name": "build-1", "argv": ["./gradlew", "build"]},
        ]
        self.assertTrue(config.validate_config(cfg))

    def test_invalid_driven_project_command_names_are_rejected(self):
        for name in ("", "has space", "../x", "x/y"):
            bad = config.deep_copy(config.DEFAULT_CONFIG)
            bad["verification"]["driven_project_commands"] = [{"name": name, "argv": ["true"]}]
            with self.assertRaises(config.ConfigError) as raised:
                config.validate_config(bad)
            self.assertIn("name", str(raised.exception))

    def test_duplicate_driven_project_command_names_are_rejected(self):
        bad = config.deep_copy(config.DEFAULT_CONFIG)
        bad["verification"]["driven_project_commands"] = [
            {"name": "unit", "argv": ["true"]},
            {"name": "unit", "argv": ["true"]},
        ]
        with self.assertRaises(config.ConfigError) as raised:
            config.validate_config(bad)
        self.assertIn("duplicated", str(raised.exception))

    def test_driven_project_command_argv_must_be_non_empty_string_list(self):
        for argv in ([], "true", [1], ["true", 2]):
            bad = config.deep_copy(config.DEFAULT_CONFIG)
            bad["verification"]["driven_project_commands"] = [{"name": "unit", "argv": argv}]
            with self.assertRaises(config.ConfigError) as raised:
                config.validate_config(bad)
            self.assertIn("argv", str(raised.exception))

    def test_driven_project_command_timeout_must_be_positive_integer_not_bool(self):
        for timeout in (True, "30", 0, -1):
            bad = config.deep_copy(config.DEFAULT_CONFIG)
            bad["verification"]["driven_project_commands"] = [{"name": "unit", "argv": ["true"], "timeout_seconds": timeout}]
            with self.assertRaises(config.ConfigError) as raised:
                config.validate_config(bad)
            self.assertIn("timeout_seconds", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
