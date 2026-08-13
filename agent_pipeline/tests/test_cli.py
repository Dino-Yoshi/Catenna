from __future__ import print_function

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

from agent_pipeline import cli
from agent_pipeline.failures import EXIT_BAD_INPUT, EXIT_SUCCESS


class CliParserTests(unittest.TestCase):
    def patch_argv0(self, argv0):
        original = cli.sys.argv
        cli.sys.argv = [argv0]
        self.addCleanup(lambda: setattr(cli.sys, "argv", original))

    def test_parser_prog_uses_module_form_by_default(self):
        self.patch_argv0("/usr/bin/python3")
        parser = cli.build_parser()
        help_text = parser.format_help()

        self.assertNotIn("tools.agent_pipeline.cli", help_text)
        self.assertIn("python3 -m agent_pipeline.cli", help_text)

    def test_parser_prog_uses_catenna_for_console_script(self):
        self.patch_argv0("/usr/local/bin/catenna")
        parser = cli.build_parser()

        self.assertIn("usage: catenna", parser.format_help())

    def test_help_for_command_uses_console_script_prog(self):
        self.patch_argv0("/usr/local/bin/catenna")
        parser = cli.build_parser()
        sub = next(a for a in parser._subparsers._group_actions if a.dest == "command")
        output = io.StringIO()

        with redirect_stdout(output):
            code = cli.print_help_for(parser, sub, "verify")

        self.assertEqual(code, EXIT_SUCCESS)
        self.assertIn("usage: catenna verify", output.getvalue())

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

    def test_run_background_parses(self):
        parser = cli.build_parser()

        args = parser.parse_args(["run", "some-task", "--background"])

        self.assertEqual(args.command, "run")
        self.assertEqual(args.task, "some-task")
        self.assertTrue(args.background)

    def test_run_bg_alias_parses_to_background(self):
        parser = cli.build_parser()

        args = parser.parse_args(["run", "some-task", "--bg"])

        self.assertEqual(args.command, "run")
        self.assertEqual(args.task, "some-task")
        self.assertTrue(args.background)

    def test_verify_background_parses(self):
        parser = cli.build_parser()

        args = parser.parse_args(["verify", "some-task", "--background"])

        self.assertEqual(args.command, "verify")
        self.assertEqual(args.task, "some-task")
        self.assertTrue(args.background)

    def test_verify_bg_alias_parses_to_background(self):
        parser = cli.build_parser()

        args = parser.parse_args(["verify", "some-task", "--bg"])

        self.assertEqual(args.command, "verify")
        self.assertEqual(args.task, "some-task")
        self.assertTrue(args.background)

    def test_tail_verbose_parses_without_short_v(self):
        parser = cli.build_parser()

        args = parser.parse_args(["tail", "some-task", "--verbose"])

        self.assertEqual(args.task, "some-task")
        self.assertTrue(args.verbose)

        with self.assertRaises(SystemExit):
            parser.parse_args(["tail", "some-task", "-v"])

    def test_brief_verbose_parses_without_short_v(self):
        parser = cli.build_parser()

        args = parser.parse_args(["brief", "some-task", "--verbose"])

        self.assertEqual(args.task, "some-task")
        self.assertTrue(args.verbose)

        with self.assertRaises(SystemExit):
            parser.parse_args(["brief", "some-task", "-v"])

    def test_background_is_only_on_run_and_verify(self):
        parser = cli.build_parser()
        err = io.StringIO()

        with redirect_stderr(err):
            with self.assertRaises(SystemExit):
                parser.parse_args(["status", "some-task", "--background"])

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

    def test_init_is_repo_level_command_without_task(self):
        parser = cli.build_parser()

        args = parser.parse_args(["init"])

        self.assertEqual(args.command, "init")
        self.assertFalse(args.force)
        self.assertFalse(hasattr(args, "task"))

    def test_init_force_parses(self):
        parser = cli.build_parser()

        args = parser.parse_args(["init", "--force"])

        self.assertEqual(args.command, "init")
        self.assertTrue(args.force)

    def test_init_rejects_positional_task(self):
        parser = cli.build_parser()
        err = io.StringIO()

        with redirect_stderr(err):
            with self.assertRaises(SystemExit):
                parser.parse_args(["init", "some-task"])

    def test_init_calls_controller_pipeline_init(self):
        calls = []
        original = cli.controller.pipeline_init
        cli.controller.pipeline_init = lambda force=False: calls.append(force) or EXIT_SUCCESS
        self.addCleanup(lambda: setattr(cli.controller, "pipeline_init", original))

        code = cli.main(["init", "--force"])

        self.assertEqual(code, EXIT_SUCCESS)
        self.assertEqual(calls, [True])

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

    def test_completion_bash_includes_bg_alias_for_run_and_verify(self):
        parser = cli.build_parser()
        sub = next(a for a in parser._subparsers._group_actions if a.dest == "command")

        script = cli.build_completion_bash(sub)

        self.assertIn("--bg", script)
        self.assertIn("--background", script)

    def test_completion_bash_includes_init_force(self):
        parser = cli.build_parser()
        sub = next(a for a in parser._subparsers._group_actions if a.dest == "command")

        script = cli.build_completion_bash(sub)

        self.assertIn("init)", script)
        self.assertIn("--force", script)


if __name__ == "__main__":
    unittest.main()
