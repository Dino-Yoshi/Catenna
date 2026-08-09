from __future__ import print_function

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_pipeline import real_runner
from agent_pipeline.real_runner import build_argv, classify, extract_candidate, run_to_files


class BuildArgvCodexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.prompt_path = self.root / "prompt.txt"
        self.prompt_path.write_text("do the thing\n", encoding="utf-8")
        self.candidate_path = self.root / "out.candidate.md"
        self.config = {"turn_budgets": {"04": 20}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_only_uses_read_sandbox_and_read_args(self):
        detail = {"command": "codex", "read_args": ["--extra-read"], "write_args": ["--extra-write"]}
        argv, metadata = build_argv("codex", detail, "read-only", self.prompt_path, self.candidate_path, self.config, "04")
        self.assertIn("--sandbox", argv)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertIn("--extra-read", argv)
        self.assertNotIn("--extra-write", argv)
        self.assertEqual(argv[0], "codex")
        self.assertIn("--json", argv)
        self.assertEqual(argv[-1], "-")
        self.assertEqual(argv, metadata)

    def test_workspace_write_uses_write_sandbox_and_write_args(self):
        detail = {"command": "codex", "read_args": ["--extra-read"], "write_args": ["--extra-write"]}
        argv, metadata = build_argv("codex", detail, "workspace-write", self.prompt_path, self.candidate_path, self.config, "05")
        self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")
        self.assertIn("--extra-write", argv)
        self.assertNotIn("--extra-read", argv)

    def test_output_last_message_points_at_candidate_path(self):
        detail = {"command": "codex", "read_args": [], "write_args": []}
        argv, _ = build_argv("codex", detail, "read-only", self.prompt_path, self.candidate_path, self.config, "04")
        self.assertEqual(argv[argv.index("--output-last-message") + 1], str(self.candidate_path))

    def test_model_is_included_before_output_last_message_and_stdin_marker_stays_last(self):
        detail = {"command": "codex", "model": "gpt-5-mini", "read_args": [], "write_args": []}
        argv, _ = build_argv("codex", detail, "read-only", self.prompt_path, self.candidate_path, self.config, "04")
        self.assertEqual(argv[argv.index("--model") + 1], "gpt-5-mini")
        self.assertLess(argv.index("--model"), argv.index("--output-last-message"))
        self.assertEqual(argv[-1], "-")


class BuildArgvClaudeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.prompt_path = self.root / "prompt.txt"
        self.prompt_path.write_text("prompt body text", encoding="utf-8")
        self.candidate_path = self.root / "out.candidate.md"
        self.config = {"turn_budgets": {"04": 20}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_only_uses_plan_mode_and_read_effort(self):
        detail = {"command": "claude", "read_effort": "high", "write_effort": "low", "read_args": [], "write_args": []}
        argv, metadata = build_argv("claude", detail, "read-only", self.prompt_path, self.candidate_path, self.config, "04")
        self.assertIn("plan", argv)
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertIn("--output-format", argv)
        self.assertIn("stream-json", argv)

    def test_workspace_write_uses_accept_edits_and_write_effort(self):
        detail = {"command": "claude", "read_effort": "high", "write_effort": "low", "read_args": [], "write_args": []}
        argv, _ = build_argv("claude", detail, "workspace-write", self.prompt_path, self.candidate_path, self.config, "05")
        self.assertIn("acceptEdits", argv)
        self.assertEqual(argv[argv.index("--effort") + 1], "low")

    def test_model_is_included_when_configured(self):
        detail = {"command": "claude", "model": "opus", "read_effort": "medium", "write_effort": "medium", "read_args": [], "write_args": []}
        argv, _ = build_argv("claude", detail, "read-only", self.prompt_path, self.candidate_path, self.config, "04")
        self.assertEqual(argv[argv.index("--model") + 1], "opus")

    def test_metadata_redacts_prompt_text_but_argv_carries_it(self):
        detail = {"command": "claude", "read_effort": "medium", "write_effort": "medium", "read_args": [], "write_args": []}
        argv, metadata = build_argv("claude", detail, "read-only", self.prompt_path, self.candidate_path, self.config, "04")
        self.assertEqual(argv[-1], "prompt body text")
        self.assertNotEqual(metadata[-1], "prompt body text")
        self.assertIn(str(self.prompt_path), metadata[-1])
        self.assertEqual(metadata[:-1], argv[:-1])


class BuildArgvAgyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.prompt_path = self.root / "prompt.txt"
        self.prompt_path.write_text("agy prompt body", encoding="utf-8")
        self.candidate_path = self.root / "out.candidate.md"
        self.config = {"turn_budgets": {"04": 20}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_print_mode_passes_prompt_via_dash_p(self):
        detail = {"command": "agy", "prompt_mode": "print", "read_args": [], "write_args": [], "common_args": []}
        argv, metadata = build_argv("agy", detail, "read-only", self.prompt_path, self.candidate_path, self.config, "04")
        self.assertEqual(argv[argv.index("-p") + 1], "agy prompt body")
        self.assertNotIn("agy prompt body", metadata)

    def test_prompt_mode_uses_dash_dash_prompt_flag(self):
        detail = {"command": "agy", "prompt_mode": "prompt", "read_args": [], "write_args": [], "common_args": []}
        argv, _ = build_argv("agy", detail, "read-only", self.prompt_path, self.candidate_path, self.config, "04")
        self.assertEqual(argv[argv.index("--prompt") + 1], "agy prompt body")

    def test_stdin_mode_requires_explicit_allow(self):
        detail = {"command": "agy", "prompt_mode": "stdin", "stdin_mode_allowed": False, "read_args": [], "write_args": [], "common_args": []}
        with self.assertRaises(Exception):
            build_argv("agy", detail, "read-only", self.prompt_path, self.candidate_path, self.config, "04")

    def test_stdin_mode_allowed_returns_redirect_hint(self):
        detail = {"command": "agy", "prompt_mode": "stdin", "stdin_mode_allowed": True, "read_args": [], "write_args": [], "common_args": []}
        argv, metadata = build_argv("agy", detail, "read-only", self.prompt_path, self.candidate_path, self.config, "04")
        self.assertEqual(metadata[-2:], ["<", str(self.prompt_path)])

    def test_workspace_write_requires_capability_enabled(self):
        detail = {"command": "agy", "prompt_mode": "print", "read_args": [], "write_args": [], "common_args": [], "workspace_write": False}
        with self.assertRaises(Exception):
            build_argv("agy", detail, "workspace-write", self.prompt_path, self.candidate_path, self.config, "05")

    def test_workspace_write_allowed_when_capability_enabled(self):
        detail = {"command": "agy", "prompt_mode": "print", "read_args": [], "write_args": ["--yolo"], "common_args": [], "workspace_write": True}
        argv, _ = build_argv("agy", detail, "workspace-write", self.prompt_path, self.candidate_path, self.config, "05")
        self.assertIn("--yolo", argv)


class ClassifyTests(unittest.TestCase):
    def test_zero_exit_is_no_failure(self):
        self.assertIsNone(classify(0, "all good", "", agent=None))

    def test_interrupted_exit_codes(self):
        self.assertEqual(classify(130, "", "", agent=None), "process_interrupted")
        self.assertEqual(classify(-2, "", "", agent=None), "process_interrupted")

    def test_max_turns_substring(self):
        self.assertEqual(classify(1, "hit the max turns limit", "", agent=None), "unknown_failure")
        self.assertEqual(classify(1, "", "maximum turns exceeded", agent=None), "max_turns")

    def test_usage_limit_substring(self):
        self.assertEqual(classify(1, "usage limit reached", "", agent=None), "unknown_failure")
        self.assertEqual(classify(1, "", "usage limit reached", agent=None), "usage_limit")
        self.assertEqual(classify(1, "", "billing issue", agent=None), "usage_limit")

    def test_rate_limit_substring(self):
        self.assertEqual(classify(1, "rate limit hit", "", agent=None), "unknown_failure")
        self.assertEqual(classify(1, "", "rate limit hit", agent=None), "rate_limit")
        self.assertEqual(classify(1, "", "too many requests", agent=None), "rate_limit")

    def test_permission_substring(self):
        self.assertEqual(classify(1, "permission denied", "", agent=None), "unknown_failure")
        self.assertEqual(classify(1, "", "permission denied", agent=None), "permission_error")
        self.assertEqual(classify(1, "", "operation not permitted", agent=None), "permission_error")
        self.assertEqual(classify(1, "", "permission requested", agent=None), "unknown_failure")
        self.assertEqual(classify(1, "", "request denied", agent=None), "unknown_failure")

    def test_sandbox_substring(self):
        self.assertEqual(classify(1, "sandbox violation", "", agent=None), "unknown_failure")
        self.assertEqual(classify(1, "", "sandbox violation", agent=None), "sandbox_environment")

    def test_negative_one_without_other_signal_is_timeout(self):
        self.assertEqual(classify(-1, "nothing recognizable", "", agent=None), "timeout")

    def test_timeout_exit_code_precedes_stderr_fallback(self):
        self.assertEqual(classify(-1, "", "usage limit reached", agent=None), "timeout")

    def test_unrecognized_nonzero_exit_is_unknown_failure(self):
        self.assertEqual(classify(1, "totally unrelated output", "", agent=None), "unknown_failure")

    def test_structured_claude_failure_takes_priority_over_substrings(self):
        stdout = '{"type":"result","subtype":"error_max_turns","is_error":true}'
        self.assertEqual(classify(1, stdout, "", agent="claude"), "max_turns")

    def test_classify_uses_supplied_events(self):
        events = [{"type": "result", "subtype": "error_max_turns", "is_error": True}]
        self.assertEqual(classify(1, "not json", "", agent="claude", events=events), "max_turns")

    def test_structured_stdout_still_classifies_with_empty_stderr(self):
        stdout = '{"type":"result","subtype":"error_max_turns","is_error":true}'
        self.assertEqual(classify(1, stdout, "", agent="claude"), "max_turns")


class RunToFilesTests(unittest.TestCase):
    def test_on_launch_runs_after_popen_returns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            launched = []
            code, timed_out = run_to_files(
                ["python3", "-c", "print('ok')"],
                root / "stdout.txt",
                root / "stderr.txt",
                30,
                on_launch=lambda: launched.append(True),
            )
            self.assertEqual(code, 0)
            self.assertFalse(timed_out)
            self.assertEqual(launched, [True])

    def test_on_launch_not_called_when_popen_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            launched = []
            with mock.patch.object(real_runner.subprocess, "Popen", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    run_to_files(
                        ["python3", "-c", "print('ok')"],
                        root / "stdout.txt",
                        root / "stderr.txt",
                        30,
                        on_launch=lambda: launched.append(True),
                    )
            self.assertEqual(launched, [])

    def test_timeout_kills_child_process_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child_pid_path = root / "child.pid"
            code = (
                "import subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                "open(sys.argv[1], 'w').write(str(child.pid))\n"
                "time.sleep(60)\n"
            )

            exit_code, timed_out = run_to_files(
                ["python3", "-c", code, str(child_pid_path)],
                root / "stdout.txt",
                root / "stderr.txt",
                1,
            )

            self.assertEqual(exit_code, -1)
            self.assertTrue(timed_out)
            self.assertTrue(child_pid_path.exists())
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            deadline = time.time() + 5
            while time.time() < deadline and self.pid_is_live(child_pid):
                time.sleep(0.05)
            if self.pid_is_live(child_pid):
                try:
                    real_runner.os.kill(child_pid, real_runner.signal.SIGKILL)
                except Exception:
                    pass
                self.fail("timed-out child process was still live")

    def test_timeout_falls_back_when_process_group_kill_fails(self):
        process = mock.Mock()
        process.pid = 12345
        process.communicate.side_effect = [real_runner.subprocess.TimeoutExpired(["cmd"], 1), (b"", b"")]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(real_runner.subprocess, "Popen", return_value=process):
                with mock.patch.object(real_runner.os, "killpg", side_effect=OSError("no pg")):
                    exit_code, timed_out = run_to_files(["cmd"], root / "stdout.txt", root / "stderr.txt", 1)

        self.assertEqual((exit_code, timed_out), (-1, True))
        process.kill.assert_called_once_with()

    def pid_is_live(self, pid):
        try:
            real_runner.os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True


class ExtractCandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_existing_nonempty_candidate_is_left_untouched(self):
        path = self.root / "candidate.md"
        path.write_text("already written by codex --output-last-message", encoding="utf-8")
        result = extract_candidate(path, "some stdout that should be ignored", agent=None)
        self.assertEqual(result, path)
        self.assertEqual(path.read_text(encoding="utf-8"), "already written by codex --output-last-message")

    def test_missing_candidate_falls_back_to_raw_stdout_when_not_json(self):
        path = self.root / "candidate.md"
        result = extract_candidate(path, "plain text response, no json here", agent=None)
        self.assertEqual(result, path)
        self.assertEqual(path.read_text(encoding="utf-8"), "plain text response, no json here")

    def test_missing_candidate_extracts_final_text_from_claude_stream(self):
        path = self.root / "candidate.md"
        stdout = '{"type":"system","subtype":"init"}\n{"type":"result","subtype":"success","is_error":false,"result":"the answer"}\n'
        result = extract_candidate(path, stdout, agent="claude")
        self.assertEqual(result, path)
        self.assertEqual(path.read_text(encoding="utf-8"), "the answer")


class InvokeAgentOverrideTests(unittest.TestCase):
    def test_role_overrides_apply_per_call_without_mutating_agent_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt_path = root / "prompt.txt"
            prompt_path.write_text("prompt\n", encoding="utf-8")
            candidate_path = root / "candidate.md"
            cfg = {
                "timeout_seconds": 30,
                "turn_budgets": {"04": 20},
                "roles": {"04": {"primary": "codex", "model_override": "stage-model", "effort_override": "low"}},
                "agents": {"codex": {"command": "codex", "model": "base-model", "read_args": [], "write_args": [], "workspace_write": True, "enabled": True}},
            }
            original_command_available = real_runner.command_available
            original_run_to_files = real_runner.run_to_files

            def fake_run_to_files(argv, stdout_path, stderr_path, timeout_seconds, stdin_text=None, on_launch=None, **kwargs):
                if on_launch:
                    on_launch()
                Path(stdout_path).write_text("", encoding="utf-8")
                Path(stderr_path).write_text("", encoding="utf-8")
                candidate_path.write_text("# Stage 4 - Final implementation brief\n", encoding="utf-8")
                return 0, False

            real_runner.command_available = lambda command: True
            real_runner.run_to_files = fake_run_to_files
            self.addCleanup(lambda: setattr(real_runner, "command_available", original_command_available))
            self.addCleanup(lambda: setattr(real_runner, "run_to_files", original_run_to_files))

            result = real_runner.invoke_agent(root, cfg, "codex", "04", "read-only", prompt_path, candidate_path, "run-1")

            self.assertEqual(result["model"], "stage-model")
            self.assertEqual(result["command_argv"][result["command_argv"].index("--model") + 1], "stage-model")
            self.assertEqual(cfg["agents"]["codex"]["model"], "base-model")
            self.assertNotIn("read_effort", cfg["agents"]["codex"])

            candidate_path_02 = root / "candidate-02.md"

            def fake_run_to_files_02(argv, stdout_path, stderr_path, timeout_seconds, stdin_text=None, on_launch=None, **kwargs):
                if on_launch:
                    on_launch()
                Path(stdout_path).write_text("", encoding="utf-8")
                Path(stderr_path).write_text("", encoding="utf-8")
                candidate_path_02.write_text("# Stage 2 - Technical specification\n", encoding="utf-8")
                return 0, False

            real_runner.run_to_files = fake_run_to_files_02
            cfg["roles"]["02"] = {"primary": "codex"}
            cfg["turn_budgets"]["02"] = 20

            result_02 = real_runner.invoke_agent(root, cfg, "codex", "02", "read-only", prompt_path, candidate_path_02, "run-2")

            self.assertEqual(result_02.get("model"), "base-model")
            self.assertEqual(result_02["command_argv"][result_02["command_argv"].index("--model") + 1], "base-model")
            self.assertEqual(cfg["agents"]["codex"]["model"], "base-model")


if __name__ == "__main__":
    unittest.main()
