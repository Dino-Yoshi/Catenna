from __future__ import print_function

import unittest

from agent_pipeline import color


class FakeStream(object):
    def __init__(self, is_a_tty):
        self._is_a_tty = is_a_tty

    def isatty(self):
        return self._is_a_tty


class ColorEnabledTests(unittest.TestCase):
    def with_env(self, **kwargs):
        import os

        originals = {key: os.environ.get(key) for key in kwargs}
        for key, value in kwargs.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

        def restore():
            for key, value in originals.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)

    def test_enabled_true_when_tty_and_no_overrides(self):
        self.with_env(NO_COLOR=None, FORCE_COLOR=None)
        self.assertTrue(color.enabled(FakeStream(True)))

    def test_enabled_false_when_not_a_tty(self):
        self.with_env(NO_COLOR=None, FORCE_COLOR=None)
        self.assertFalse(color.enabled(FakeStream(False)))

    def test_no_color_disables_even_on_tty(self):
        self.with_env(NO_COLOR="1", FORCE_COLOR=None)
        self.assertFalse(color.enabled(FakeStream(True)))

    def test_force_color_enables_even_off_tty(self):
        self.with_env(NO_COLOR=None, FORCE_COLOR="1")
        self.assertTrue(color.enabled(FakeStream(False)))

    def test_no_color_takes_precedence_over_force_color(self):
        self.with_env(NO_COLOR="1", FORCE_COLOR="1")
        self.assertFalse(color.enabled(FakeStream(True)))


class ColorWrapTests(unittest.TestCase):
    def with_env(self, **kwargs):
        import os

        originals = {key: os.environ.get(key) for key in kwargs}
        for key, value in kwargs.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

        def restore():
            for key, value in originals.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)

    def test_wrap_noop_when_disabled(self):
        self.with_env(NO_COLOR=None, FORCE_COLOR=None)
        self.assertEqual(color.wrap("red", "hello", FakeStream(False)), "hello")

    def test_wrap_brackets_with_escape_codes_when_enabled(self):
        self.with_env(NO_COLOR=None, FORCE_COLOR=None)
        self.assertEqual(color.wrap("red", "hello", FakeStream(True)), "\x1b[31mhello\x1b[0m")

    def test_named_helpers_use_expected_codes(self):
        self.with_env(NO_COLOR=None, FORCE_COLOR=None)
        tty = FakeStream(True)
        self.assertEqual(color.red("x", tty), "\x1b[31mx\x1b[0m")
        self.assertEqual(color.green("x", tty), "\x1b[32mx\x1b[0m")
        self.assertEqual(color.yellow("x", tty), "\x1b[33mx\x1b[0m")
        self.assertEqual(color.cyan("x", tty), "\x1b[36mx\x1b[0m")
        self.assertEqual(color.dim("x", tty), "\x1b[2mx\x1b[0m")
        self.assertEqual(color.bold("x", tty), "\x1b[1mx\x1b[0m")

    def test_colorize_state_known_state(self):
        self.with_env(NO_COLOR=None, FORCE_COLOR=None)
        self.assertEqual(color.colorize_state("failed", FakeStream(True)), "\x1b[31mfailed\x1b[0m")
        self.assertEqual(color.colorize_state("complete", FakeStream(True)), "\x1b[32mcomplete\x1b[0m")

    def test_colorize_state_unknown_state_falls_back_to_plain(self):
        self.with_env(NO_COLOR=None, FORCE_COLOR=None)
        self.assertEqual(color.colorize_state("some_unmapped_state", FakeStream(True)), "some_unmapped_state")


if __name__ == "__main__":
    unittest.main()
