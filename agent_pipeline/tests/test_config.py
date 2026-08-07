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


if __name__ == "__main__":
    unittest.main()
