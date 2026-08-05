from __future__ import print_function

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent_pipeline import tail
from agent_pipeline.state import orchestrator_dir


CLAUDE_LINES = [
    '{"type":"system","subtype":"init","cwd":"/repo"}',
    '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"result":"final text here"}',
]


class TailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.task_dir = Path(self.tmp.name) / "task"
        self.task_dir.mkdir(parents=True)
        self.runs_dir = orchestrator_dir(self.task_dir) / "runs"
        self.runs_dir.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write_run(self, base, lines, with_metadata=True, mtime_offset=0):
        stdout_path = self.runs_dir / (base + ".stdout")
        stdout_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if mtime_offset:
            stamp = time.time() + mtime_offset
            import os

            os.utime(str(stdout_path), (stamp, stamp))
        if with_metadata:
            metadata_path = self.runs_dir / (base + ".json")
            metadata_path.write_text(
                json.dumps({"stage": "04", "duration_seconds": 1.5, "exit_code": 0, "failure_class": None}),
                encoding="utf-8",
            )
        return stdout_path

    def test_locate_prefers_in_progress_run(self):
        self.write_run("04-pass-1-attempt-1-claude-run-a", CLAUDE_LINES, with_metadata=True, mtime_offset=-10)
        in_progress = self.write_run("05-pass-1-attempt-1-codex-run-b", CLAUDE_LINES, with_metadata=False)
        found = tail.locate(self.task_dir)
        self.assertEqual(found, in_progress)

    def test_locate_falls_back_to_newest_completed(self):
        older = self.write_run("04-pass-1-attempt-1-claude-run-a", CLAUDE_LINES, with_metadata=True, mtime_offset=-10)
        newer = self.write_run("05-pass-1-attempt-1-codex-run-b", CLAUDE_LINES, with_metadata=True)
        found = tail.locate(self.task_dir)
        self.assertEqual(found, newer)
        self.assertNotEqual(found, older)

    def test_locate_filters_by_stage(self):
        self.write_run("04-pass-1-attempt-1-claude-run-a", CLAUDE_LINES)
        target = self.write_run("05-pass-1-attempt-1-codex-run-b", CLAUDE_LINES)
        found = tail.locate(self.task_dir, stage="05")
        self.assertEqual(found, target)

    def test_locate_filters_by_run_id(self):
        self.write_run("04-pass-1-attempt-1-claude-run-a", CLAUDE_LINES)
        target = self.write_run("05-pass-1-attempt-1-codex-run-b", CLAUDE_LINES)
        found = tail.locate(self.task_dir, run_id="run-b")
        self.assertEqual(found, target)

    def test_locate_returns_none_when_no_runs(self):
        self.assertIsNone(tail.locate(self.task_dir))

    def test_brief_reports_metadata_and_final_text(self):
        self.write_run("04-pass-1-attempt-1-claude-run-a", CLAUDE_LINES, with_metadata=True)
        lines = []
        result = tail.brief(self.task_dir, print_fn=lines.append)
        self.assertEqual(result, "ok")
        joined = "\n".join(lines)
        self.assertIn("claude", joined)
        self.assertIn("final text here", joined)
        self.assertIn("failure_class: none", joined)

    def test_brief_reports_usage_when_present_in_metadata_sidecar(self):
        stdout_path = self.runs_dir / "04-pass-1-attempt-1-claude-run-a.stdout"
        stdout_path.write_text("\n".join(CLAUDE_LINES) + "\n", encoding="utf-8")
        metadata_path = self.runs_dir / "04-pass-1-attempt-1-claude-run-a.json"
        metadata_path.write_text(
            json.dumps({"stage": "04", "duration_seconds": 1.5, "exit_code": 0, "failure_class": None, "usage": {"input_tokens": 42}}),
            encoding="utf-8",
        )
        lines = []
        result = tail.brief(self.task_dir, print_fn=lines.append)
        self.assertEqual(result, "ok")
        joined = "\n".join(lines)
        self.assertIn("usage:", joined)
        self.assertIn("42", joined)

    def test_brief_omits_usage_line_when_absent_from_metadata(self):
        self.write_run("04-pass-1-attempt-1-claude-run-a", CLAUDE_LINES, with_metadata=True)
        lines = []
        tail.brief(self.task_dir, print_fn=lines.append)
        joined = "\n".join(lines)
        self.assertNotIn("usage:", joined)

    def test_brief_reports_reasoning_when_present_in_metadata_sidecar(self):
        stdout_path = self.runs_dir / "04-pass-1-attempt-1-codex-run-a.stdout"
        stdout_path.write_text("\n".join(CLAUDE_LINES) + "\n", encoding="utf-8")
        reasoning_path = self.runs_dir / "04-pass-1-attempt-1-codex-run-a.reasoning.md"
        reasoning_path.write_text("# Reasoning trace\n\nfirst I checked the schema\n", encoding="utf-8")
        metadata_path = self.runs_dir / "04-pass-1-attempt-1-codex-run-a.json"
        metadata_path.write_text(
            json.dumps({"stage": "04", "duration_seconds": 1.5, "exit_code": 0, "failure_class": None, "reasoning_path": str(reasoning_path)}),
            encoding="utf-8",
        )
        lines = []
        result = tail.brief(self.task_dir, print_fn=lines.append)
        self.assertEqual(result, "ok")
        joined = "\n".join(lines)
        self.assertIn("reasoning_path:", joined)
        self.assertIn("reasoning: ", joined)
        self.assertIn("first I checked the schema", joined)

    def test_brief_omits_reasoning_lines_when_absent_from_metadata(self):
        self.write_run("04-pass-1-attempt-1-claude-run-a", CLAUDE_LINES, with_metadata=True)
        lines = []
        tail.brief(self.task_dir, print_fn=lines.append)
        joined = "\n".join(lines)
        self.assertNotIn("reasoning:", joined)
        self.assertNotIn("reasoning_path:", joined)

    def test_brief_does_not_raise_when_reasoning_file_is_missing(self):
        stdout_path = self.runs_dir / "04-pass-1-attempt-1-codex-run-a.stdout"
        stdout_path.write_text("\n".join(CLAUDE_LINES) + "\n", encoding="utf-8")
        metadata_path = self.runs_dir / "04-pass-1-attempt-1-codex-run-a.json"
        metadata_path.write_text(
            json.dumps({"stage": "04", "duration_seconds": 1.5, "exit_code": 0, "failure_class": None, "reasoning_path": str(self.runs_dir / "gone.reasoning.md")}),
            encoding="utf-8",
        )
        lines = []
        result = tail.brief(self.task_dir, print_fn=lines.append)
        self.assertEqual(result, "ok")
        joined = "\n".join(lines)
        self.assertIn("reasoning_path:", joined)
        self.assertNotIn("reasoning: ", joined)

    def test_brief_handles_no_runs(self):
        lines = []
        result = tail.brief(self.task_dir, print_fn=lines.append)
        self.assertEqual(result, "no_runs")
        self.assertIn("no runs found for this task yet", lines)

    def test_follow_prints_new_lines_as_they_are_appended_and_stops_on_completion(self):
        stdout_path = self.runs_dir / "04-pass-1-attempt-1-claude-run-a.stdout"
        stdout_path.write_text(CLAUDE_LINES[0] + "\n", encoding="utf-8")
        metadata_path = self.runs_dir / "04-pass-1-attempt-1-claude-run-a.json"

        def append_and_finish():
            time.sleep(0.2)
            with open(str(stdout_path), "a", encoding="utf-8") as handle:
                handle.write(CLAUDE_LINES[1] + "\n")
            time.sleep(0.2)
            metadata_path.write_text(json.dumps({"stage": "04"}), encoding="utf-8")

        thread = threading.Thread(target=append_and_finish)
        thread.start()
        lines = []
        result = tail.follow(self.task_dir, poll_interval=0.05, print_fn=lines.append, max_wait_seconds=5)
        thread.join()

        self.assertEqual(result, "complete")
        joined = "\n".join(lines)
        self.assertIn("final text here", joined)
        self.assertIn("run complete", joined)

    def test_follow_times_out_if_run_never_completes(self):
        self.runs_dir.joinpath("04-pass-1-attempt-1-claude-run-a.stdout").write_text(
            CLAUDE_LINES[0] + "\n", encoding="utf-8"
        )
        lines = []
        result = tail.follow(self.task_dir, poll_interval=0.05, print_fn=lines.append, max_wait_seconds=0.2)
        self.assertEqual(result, "timed_out")

    def test_follow_handles_no_runs(self):
        lines = []
        result = tail.follow(self.task_dir, print_fn=lines.append)
        self.assertEqual(result, "no_runs")


if __name__ == "__main__":
    unittest.main()
