from __future__ import print_function

import unittest

from agent_pipeline import cli


class CliParserTests(unittest.TestCase):
    def test_parser_prog_uses_current_module_path(self):
        parser = cli.build_parser()
        help_text = parser.format_help()

        self.assertNotIn("tools.agent_pipeline.cli", help_text)
        self.assertIn("python3 -m agent_pipeline.cli", help_text)

    def test_task_is_positional_and_optional_on_renamed_command(self):
        parser = cli.build_parser()

        args = parser.parse_args(["verify", "some-task"])
        self.assertEqual(args.task, "some-task")

        args = parser.parse_args(["verify"])
        self.assertIsNone(args.task)

    def test_run_command_dropped_pipeline_prefix(self):
        parser = cli.build_parser()
        args = parser.parse_args(["run", "some-task", "--allow-dirty"])
        self.assertEqual(args.command, "run")
        self.assertEqual(args.task, "some-task")
        self.assertTrue(args.allow_dirty)

    def test_use_select_set_are_aliases_of_one_parser(self):
        parser = cli.build_parser()
        funcs = set()

        for name in ("use", "select", "set"):
            args = parser.parse_args([name, "some-task"])
            self.assertEqual(args.task, "some-task")
            funcs.add(args.func)

        self.assertEqual(len(funcs), 1)

    def test_tasks_ls_are_aliases_of_one_parser(self):
        parser = cli.build_parser()
        funcs = set()

        for name in ("tasks", "ls"):
            args = parser.parse_args([name])
            funcs.add(args.func)

        self.assertEqual(len(funcs), 1)

    def test_usage_task_flag_is_independent_optional_filter(self):
        parser = cli.build_parser()

        args = parser.parse_args(["usage"])
        self.assertIsNone(args.task)

        args = parser.parse_args(["usage", "--task", "some-task"])
        self.assertEqual(args.task, "some-task")


if __name__ == "__main__":
    unittest.main()
