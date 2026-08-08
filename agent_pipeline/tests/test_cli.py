from __future__ import print_function

import io
import unittest
from contextlib import redirect_stdout

from agent_pipeline import cli
from agent_pipeline.failures import EXIT_BAD_INPUT, EXIT_SUCCESS


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

    def test_tasks_plain_flag_parses(self):
        parser = cli.build_parser()

        args = parser.parse_args(["tasks", "--plain"])
        self.assertTrue(args.plain)

        args = parser.parse_args(["tasks"])
        self.assertFalse(args.plain)


class CliHelpCommandTests(unittest.TestCase):
    def test_help_with_no_command_matches_top_level_help(self):
        parser = cli.build_parser()
        sub = next(a for a in parser._subparsers._group_actions if a.dest == "command")

        top_level = io.StringIO()
        with redirect_stdout(top_level):
            parser.print_help()

        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.print_help_for(parser, sub, None)

        self.assertEqual(code, EXIT_SUCCESS)
        self.assertEqual(output.getvalue(), top_level.getvalue())

    def test_help_for_renamed_command_matches_its_own_help_flag(self):
        parser = cli.build_parser()
        sub = next(a for a in parser._subparsers._group_actions if a.dest == "command")

        direct = io.StringIO()
        with redirect_stdout(direct):
            with self.assertRaises(SystemExit):
                parser.parse_args(["verify", "--help"])

        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.print_help_for(parser, sub, "verify")

        self.assertEqual(code, EXIT_SUCCESS)
        self.assertEqual(output.getvalue(), direct.getvalue())

    def test_help_for_alias_resolves_to_shared_subparser(self):
        parser = cli.build_parser()
        sub = next(a for a in parser._subparsers._group_actions if a.dest == "command")

        ls_output = io.StringIO()
        with redirect_stdout(ls_output):
            cli.print_help_for(parser, sub, "ls")

        tasks_output = io.StringIO()
        with redirect_stdout(tasks_output):
            cli.print_help_for(parser, sub, "tasks")

        self.assertEqual(ls_output.getvalue(), tasks_output.getvalue())

    def test_help_for_unknown_command_is_clear_error_not_traceback(self):
        parser = cli.build_parser()
        sub = next(a for a in parser._subparsers._group_actions if a.dest == "command")

        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.print_help_for(parser, sub, "bogus-command")

        self.assertEqual(code, EXIT_BAD_INPUT)
        self.assertIn("unknown command", output.getvalue())
        self.assertIn("verify", output.getvalue())


class CliCompletionCommandTests(unittest.TestCase):
    def test_completion_bash_registers_completion_function(self):
        parser = cli.build_parser()
        sub = next(a for a in parser._subparsers._group_actions if a.dest == "command")

        script = cli.build_completion_bash(sub)

        self.assertIn("complete -F _catenna_complete catenna", script)

    def test_completion_bash_mentions_every_top_level_command(self):
        parser = cli.build_parser()
        sub = next(a for a in parser._subparsers._group_actions if a.dest == "command")

        script = cli.build_completion_bash(sub)

        for name in sub.choices:
            self.assertIn(name, script)


if __name__ == "__main__":
    unittest.main()
