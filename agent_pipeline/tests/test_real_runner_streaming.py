from __future__ import print_function

import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from agent_pipeline.real_runner import invoke_agent


class StreamingRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.task_dir = self.root / "task"
        self.task_dir.mkdir(parents=True)
        self.prompt_path = self.root / "prompt.txt"
        self.prompt_path.write_text("do the thing\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def write_fake(self, body):
        path = self.root / "fake_cli.py"
        path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body), encoding="utf-8")
        path.chmod(0o755)
        return path

    def base_config(self, agent, command):
        return {
            "timeout_seconds": 30,
            "turn_budgets": {"04": 5},
            "agents": {agent: {"command": str(command), "model": None, "read_args": [], "write_args": [], "workspace_write": False}},
        }

    def test_claude_stream_json_final_text_becomes_candidate(self):
        fake = self.write_fake(
            """
            import sys
            assert "--output-format" in sys.argv and "stream-json" in sys.argv
            print('{"type":"system","subtype":"init"}')
            print('{"type":"result","subtype":"success","is_error":false,"result":"the real final answer"}')
            """
        )
        config = self.base_config("claude", fake)
        candidate_path = self.task_dir / "04-pass-1-attempt-1-claude-run-x.candidate.md"
        result = invoke_agent(self.task_dir, config, "claude", "04", "read-only", self.prompt_path, candidate_path, "run-x")

        self.assertEqual(result["exit_code"], 0)
        self.assertIsNone(result["failure_class"])
        self.assertEqual(Path(result["candidate_artifact_path"]).read_text(encoding="utf-8"), "the real final answer")

    def test_agy_stream_json_final_text_becomes_candidate(self):
        fake = self.write_fake(
            """
            import sys
            assert "--output-format" in sys.argv and "stream-json" in sys.argv
            print('{"event":"init","conversation_id":"c1"}')
            print('{"event":"result","result":{"status":"SUCCESS","response":"agy final answer"}}')
            """
        )
        config = self.base_config("agy", fake)
        config["agents"]["agy"]["prompt_mode"] = "print"
        config["agents"]["agy"]["stdin_mode_allowed"] = False
        candidate_path = self.task_dir / "04-pass-1-attempt-1-agy-run-x.candidate.md"
        result = invoke_agent(self.task_dir, config, "agy", "04", "read-only", self.prompt_path, candidate_path, "run-x")

        self.assertEqual(result["exit_code"], 0)
        self.assertIsNone(result["failure_class"])
        self.assertEqual(Path(result["candidate_artifact_path"]).read_text(encoding="utf-8"), "agy final answer")

    def test_claude_structured_max_turns_failure_is_classified(self):
        fake = self.write_fake(
            """
            import sys
            print('{"type":"system","subtype":"init"}')
            print('{"type":"result","subtype":"error_max_turns","is_error":true,"num_turns":20}')
            sys.exit(1)
            """
        )
        config = self.base_config("claude", fake)
        candidate_path = self.task_dir / "04-pass-1-attempt-1-claude-run-y.candidate.md"
        result = invoke_agent(self.task_dir, config, "claude", "04", "read-only", self.prompt_path, candidate_path, "run-y")

        self.assertEqual(result["failure_class"], "max_turns")
        self.assertTrue(result["partial"])

    def test_codex_candidate_still_comes_from_output_last_message(self):
        # codex writes the final answer to --output-last-message regardless
        # of the new --json flag; extract_candidate must prefer that file
        # over anything parsed from the (now-JSONL) stdout.
        fake = self.write_fake(
            """
            import sys
            assert "--json" in sys.argv
            out_path = sys.argv[sys.argv.index("--output-last-message") + 1]
            with open(out_path, "w") as handle:
                handle.write("codex final answer via output-last-message")
            print('{"type":"thread.started"}')
            print('{"type":"turn.completed"}')
            """
        )
        config = self.base_config("codex", fake)
        candidate_path = self.task_dir / "04-pass-1-attempt-1-codex-run-z.candidate.md"
        result = invoke_agent(self.task_dir, config, "codex", "04", "read-only", self.prompt_path, candidate_path, "run-z")

        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(Path(result["candidate_artifact_path"]).read_text(encoding="utf-8"), "codex final answer via output-last-message")

    def test_plain_text_output_without_json_still_falls_back_to_raw_stdout(self):
        # An agent CLI that ignores the streaming flags (or a legacy
        # fixture) must keep working exactly as before: raw stdout dumped
        # straight into the candidate file.
        fake = self.write_fake(
            """
            print("plain markdown body, not json at all")
            """
        )
        config = self.base_config("claude", fake)
        candidate_path = self.task_dir / "04-pass-1-attempt-1-claude-run-w.candidate.md"
        result = invoke_agent(self.task_dir, config, "claude", "04", "read-only", self.prompt_path, candidate_path, "run-w")

        self.assertEqual(Path(result["candidate_artifact_path"]).read_text(encoding="utf-8").strip(), "plain markdown body, not json at all")

    def test_usage_bearing_stream_populates_result_and_ledger(self):
        fake = self.write_fake(
            """
            print('{"type":"system","subtype":"init"}')
            print('{"type":"result","subtype":"success","is_error":false,"result":"ok",'
                  '"total_cost_usd":0.05,"usage":{"input_tokens":100,"output_tokens":20}}')
            """
        )
        config = self.base_config("claude", fake)
        candidate_path = self.task_dir / "04-pass-1-attempt-1-claude-run-usage.candidate.md"
        ledger_path = self.root / "usage" / "ledger.jsonl"
        result = invoke_agent(
            self.task_dir, config, "claude", "04", "read-only", self.prompt_path, candidate_path, "run-usage",
            task="task-usage", ledger_path=ledger_path,
        )

        self.assertEqual(result["usage"]["input_tokens"], 100)
        self.assertEqual(result["usage"]["output_tokens"], 20)
        lines = ledger_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["task"], "task-usage")
        self.assertEqual(entry["agent"], "claude")
        self.assertEqual(entry["usage"]["input_tokens"], 100)

    def test_reasoning_bearing_stream_writes_sidecar_and_result_field(self):
        fake = self.write_fake(
            """
            print('{"type":"thread.started","thread_id":"t1"}')
            print('{"type":"item.completed","item":{"id":"item_r","type":"reasoning","text":"thought about it"}}')
            print('{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"final answer"}}')
            print('{"type":"turn.completed"}')
            """
        )
        config = self.base_config("codex", fake)
        candidate_path = self.task_dir / "04-pass-1-attempt-1-codex-run-reason.candidate.md"
        result = invoke_agent(self.task_dir, config, "codex", "04", "read-only", self.prompt_path, candidate_path, "run-reason")

        self.assertIsNotNone(result["reasoning_path"])
        reasoning_path = Path(result["reasoning_path"])
        self.assertTrue(reasoning_path.exists())
        self.assertIn("thought about it", reasoning_path.read_text(encoding="utf-8"))

    def test_non_reasoning_stream_writes_no_sidecar(self):
        fake = self.write_fake(
            """
            print('{"type":"system","subtype":"init"}')
            print('{"type":"result","subtype":"success","is_error":false,"result":"ok"}')
            """
        )
        config = self.base_config("claude", fake)
        candidate_path = self.task_dir / "04-pass-1-attempt-1-claude-run-noreason.candidate.md"
        result = invoke_agent(self.task_dir, config, "claude", "04", "read-only", self.prompt_path, candidate_path, "run-noreason")

        self.assertIsNone(result["reasoning_path"])
        self.assertFalse((self.task_dir / ".orchestrator" / "runs" / "04-pass-1-attempt-1-claude-run-noreason.reasoning.md").exists())

    def test_capture_reasoning_false_computes_but_does_not_write(self):
        fake = self.write_fake(
            """
            print('{"type":"thread.started","thread_id":"t1"}')
            print('{"type":"item.completed","item":{"id":"item_r","type":"reasoning","text":"thought about it"}}')
            print('{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"final answer"}}')
            print('{"type":"turn.completed"}')
            """
        )
        config = self.base_config("codex", fake)
        candidate_path = self.task_dir / "04-pass-1-attempt-1-codex-run-nocapture.candidate.md"
        result = invoke_agent(
            self.task_dir, config, "codex", "04", "read-only", self.prompt_path, candidate_path, "run-nocapture",
            capture_reasoning=False,
        )

        self.assertIsNone(result["reasoning_path"])
        self.assertFalse((self.task_dir / ".orchestrator" / "runs" / "04-pass-1-attempt-1-codex-run-nocapture.reasoning.md").exists())

    def test_no_ledger_path_writes_nothing(self):
        fake = self.write_fake(
            """
            print('{"type":"system","subtype":"init"}')
            print('{"type":"result","subtype":"success","is_error":false,"result":"ok"}')
            """
        )
        config = self.base_config("claude", fake)
        candidate_path = self.task_dir / "04-pass-1-attempt-1-claude-run-noledger.candidate.md"
        result = invoke_agent(self.task_dir, config, "claude", "04", "read-only", self.prompt_path, candidate_path, "run-noledger")

        self.assertIsNone(result.get("failure_class"))
        self.assertFalse((self.root / "usage").exists())


if __name__ == "__main__":
    unittest.main()
