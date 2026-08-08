from __future__ import print_function

import unittest

from agent_pipeline import cli


class CliParserTests(unittest.TestCase):
    def test_parser_prog_uses_current_module_path(self):
        parser = cli.build_parser()
        help_text = parser.format_help()

        self.assertNotIn("tools.agent_pipeline.cli", help_text)
        self.assertIn("python3 -m agent_pipeline.cli", help_text)


if __name__ == "__main__":
    unittest.main()
