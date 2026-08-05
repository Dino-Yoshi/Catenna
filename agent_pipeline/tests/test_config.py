from __future__ import print_function

import unittest

from agent_pipeline import config


class ReasoningCaptureConfigTests(unittest.TestCase):
    def test_default_config_is_valid(self):
        self.assertTrue(config.validate_config(config.deep_copy(config.DEFAULT_CONFIG)))

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
