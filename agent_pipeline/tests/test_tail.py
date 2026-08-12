from __future__ import print_function

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent_pipeline import tail
from agent_pipeline.state import new_state, orchestrator_dir, write_state_atomic


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
        self.verification_runs_dir = orchestrator_dir(self.task_dir) / "verification_runs"

    def tearDown(self):
        self.tmp.cleanup()

    def write_run(self, base, lines, with_metadata=True, mtime_offset=0):
        stdout_path = self.runs_dir / (base + ".stdout")
        stdout_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if mtime_offset:
            stamp = time.time() + mtime_offset
            os.utime(str(stdout_path), (stamp, stamp))
        if with_metadata:
            metadata_path = self.runs_dir / (base + ".json")
            metadata_path.write_text(
                json.dumps({"stage": "04", "duration_seconds": 1.5, "exit_code": 0, "failure_class": None}),
                encoding="utf-8",
            )
        return stdout_path

    def write_running_sidecar(self, stdout_path, pid=None):
        payload = {"status": "running", "stdout_path": str(stdout_path)}
        if pid is not None:
            payload["pid"] = pid
            payload["host"] = socket.gethostname()
        stdout_path.with_suffix(".json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )

    def dead_pid(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return proc.pid

    def write_verification_run(self, base, text, with_metadata=True, mtime_offset=0):
        self.verification_runs_dir.mkdir(parents=True)
        stdout_path = self.verification_runs_dir / (base + ".stdout")
        stdout_path.write_text(text, encoding="utf-8")
        if mtime_offset:
            stamp = time.time() + mtime_offset
            os.utime(str(stdout_path), (stamp, stamp))
        if with_metadata:
            stdout_path.with_suffix(".json").write_text(json.dumps({"name": base, "status": "passed"}), encoding="utf-8")
        return stdout_path

    def test_locate_prefers_in_progress_run(self):
        self.write_run("04-pass-1-attempt-1-claude-run-a", CLAUDE_LINES, with_metadata=True, mtime_offset=-10)
        in_progress = self.write_run("05-pass-1-attempt-1-codex-run-b", CLAUDE_LINES, with_metadata=False)
        self.write_running_sidecar(in_progress)
        found = tail.locate(self.task_dir)
        self.assertEqual(found, in_progress)

    def test_locate_unfiltered_scans_verification_runs(self):
        self.write_run("04-pass-1-attempt-1-claude-run-a", CLAUDE_LINES, with_metadata=True, mtime_offset=-10)
        verify = self.write_verification_run("unit_tests-run-b", "ok\n", with_metadata=False)
        self.write_running_sidecar(verify)

        found = tail.locate(self.task_dir)

        self.assertEqual(found, verify)

    def test_locate_does_not_treat_missing_sidecar_as_in_progress(self):
        orphan = self.write_run("05-pass-1-attempt-1-codex-orphan", CLAUDE_LINES, with_metadata=False, mtime_offset=-10)
        completed = self.write_run("04-pass-1-attempt-1-claude-complete", CLAUDE_LINES, with_metadata=True)

        found = tail.locate(self.task_dir)

        self.assertEqual(found, completed)
        self.assertNotEqual(found, orphan)

    def test_locate_does_not_treat_dead_pid_sidecar_as_in_progress(self):
        orphan = self.write_run("05-pass-1-attempt-1-codex-orphan", CLAUDE_LINES, with_metadata=False, mtime_offset=-10)
        self.write_running_sidecar(orphan, pid=self.dead_pid())
        completed = self.write_run("04-pass-1-attempt-1-claude-complete", CLAUDE_LINES, with_metadata=True)

        found = tail.locate(self.task_dir)

        self.assertEqual(found, completed)
        self.assertNotEqual(found, orphan)

    def test_locate_filters_remain_pipeline_run_only(self):
        self.write_verification_run("05-pass-1-attempt-1-claude-run-a", "verification\n", with_metadata=False)
        target = self.write_run("05-pass-1-attempt-1-codex-run-b", CLAUDE_LINES, with_metadata=True, mtime_offset=-10)

        found = tail.locate(self.task_dir, stage="05")

        self.assertEqual(found, target)

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

    def test_brief_ignores_newer_verification_run(self):
        target = self.write_run("04-pass-1-attempt-1-claude-run-a", CLAUDE_LINES, with_metadata=True, mtime_offset=-10)
        self.write_verification_run("unit_tests-newer", "plain verification output\n", with_metadata=True)

        lines = []
        result = tail.brief(self.task_dir, print_fn=lines.append)

        self.assertEqual(result, "ok")
        self.assertIn("run: %s" % target.name, "\n".join(lines))

    def test_brief_treats_none_duration_as_zero(self):
        stdout_path = self.runs_dir / "04-pass-1-attempt-1-claude-run-none.stdout"
        stdout_path.write_text("\n".join(CLAUDE_LINES) + "\n", encoding="utf-8")
        metadata_path = self.runs_dir / "04-pass-1-attempt-1-claude-run-none.json"
        metadata_path.write_text(
            json.dumps({"stage": "04", "duration_seconds": None, "exit_code": 0, "failure_class": None}),
            encoding="utf-8",
        )
        lines = []
        result = tail.brief(self.task_dir, print_fn=lines.append)
        self.assertEqual(result, "ok")
        self.assertIn("duration: 0.0s", "\n".join(lines))

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

    def test_brief_verbose_does_not_truncate_reasoning_or_final_text(self):
        long_text = "x" * 350
        stdout_path = self.runs_dir / "04-pass-1-attempt-1-claude-run-long.stdout"
        stdout_path.write_text(
            '{"type":"system","subtype":"init"}\n'
            + json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": long_text})
            + "\n",
            encoding="utf-8",
        )
        reasoning_path = self.runs_dir / "long.reasoning.md"
        reasoning_path.write_text(long_text, encoding="utf-8")
        stdout_path.with_suffix(".json").write_text(
            json.dumps({"stage": "04", "duration_seconds": 1.5, "exit_code": 0, "failure_class": None, "reasoning_path": str(reasoning_path)}),
            encoding="utf-8",
        )

        lines = []
        result = tail.brief(self.task_dir, print_fn=lines.append, verbose=True)

        self.assertEqual(result, "ok")
        joined = "\n".join(lines)
        self.assertIn("reasoning: " + long_text, joined)
        self.assertIn("final text: " + long_text, joined)

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
        self.write_running_sidecar(stdout_path)

        def append_and_finish():
            time.sleep(0.2)
            with open(str(stdout_path), "a", encoding="utf-8") as handle:
                handle.write(CLAUDE_LINES[1] + "\n")
            time.sleep(0.2)
            metadata_path.write_text(json.dumps({"stage": "04", "exit_code": 0}), encoding="utf-8")

        thread = threading.Thread(target=append_and_finish)
        thread.start()
        lines = []
        result = tail.follow(self.task_dir, stage="04", poll_interval=0.05, print_fn=lines.append, max_wait_seconds=5)
        thread.join()

        self.assertEqual(result, "complete")
        joined = "\n".join(lines)
        self.assertIn("final text here", joined)
        self.assertIn("run complete", joined)

    def test_follow_unfiltered_advances_from_pipeline_to_verification_run(self):
        state_obj = new_state(self.task_dir.name)
        state_obj["state"] = "running"
        write_state_atomic(self.task_dir, state_obj)
        stdout_path = self.write_run("04-pass-1-attempt-1-claude-run-a", CLAUDE_LINES, with_metadata=False, mtime_offset=-10)
        stdout_path.with_suffix(".json").write_text(json.dumps({"stage": "04", "exit_code": 0}), encoding="utf-8")

        def add_verification_and_finish():
            time.sleep(0.12)
            verify_path = self.write_verification_run("unit_tests-run-b", "first line\npartial", with_metadata=False)
            self.write_running_sidecar(verify_path)
            time.sleep(0.12)
            verify_path.with_suffix(".json").write_text(json.dumps({"name": "unit_tests", "status": "passed"}), encoding="utf-8")
            (self.task_dir / "05_verification_report.md").write_text("# report\n", encoding="utf-8")

        thread = threading.Thread(target=add_verification_and_finish)
        thread.start()
        lines = []
        result = tail.follow(self.task_dir, poll_interval=0.05, print_fn=lines.append, max_wait_seconds=5)
        thread.join()

        self.assertEqual(result, "complete")
        joined = "\n".join(lines)
        self.assertIn("-- following unit_tests-run-b.stdout", joined)
        self.assertIn("first line", joined)
        self.assertIn("partial", joined)
        self.assertIn("verification complete", joined)

    def test_follow_unfiltered_completed_pipeline_stops_on_paused_state(self):
        state_obj = new_state(self.task_dir.name)
        state_obj["state"] = "awaiting_human_test"
        write_state_atomic(self.task_dir, state_obj)
        stdout_path = self.write_run("04-pass-1-attempt-1-claude-run-a", CLAUDE_LINES, with_metadata=False)
        stdout_path.with_suffix(".json").write_text(json.dumps({"stage": "04", "exit_code": 0}), encoding="utf-8")

        lines = []
        result = tail.follow(self.task_dir, poll_interval=0.01, print_fn=lines.append, max_wait_seconds=1)

        self.assertEqual(result, "complete")
        self.assertIn("pipeline paused: state=awaiting_human_test", "\n".join(lines))

    def test_follow_unfiltered_completed_pipeline_stops_on_terminal_state(self):
        # "blocked"/"failed" (unlike "complete") aren't remapped by
        # reconcile_artifacts when the task dir has no seed artifacts on
        # disk, so this doesn't need a full artifact set to stay accurate.
        state_obj = new_state(self.task_dir.name)
        state_obj["state"] = "blocked"
        write_state_atomic(self.task_dir, state_obj)
        stdout_path = self.write_run("04-pass-1-attempt-1-claude-run-a", CLAUDE_LINES, with_metadata=False)
        stdout_path.with_suffix(".json").write_text(json.dumps({"stage": "04", "exit_code": 0}), encoding="utf-8")

        lines = []
        result = tail.follow(self.task_dir, poll_interval=0.01, print_fn=lines.append, max_wait_seconds=1)

        self.assertEqual(result, "complete")
        self.assertIn("pipeline finished: state=blocked", "\n".join(lines))

    def test_follow_unfiltered_reports_unreadable_state_and_stops(self):
        orchestrator_dir(self.task_dir).mkdir(parents=True, exist_ok=True)
        (orchestrator_dir(self.task_dir) / "state.json").write_text("not json", encoding="utf-8")
        stdout_path = self.write_run("04-pass-1-attempt-1-claude-run-a", CLAUDE_LINES, with_metadata=False)
        stdout_path.with_suffix(".json").write_text(json.dumps({"stage": "04", "exit_code": 0}), encoding="utf-8")

        lines = []
        result = tail.follow(self.task_dir, poll_interval=0.01, print_fn=lines.append, max_wait_seconds=1)

        self.assertEqual(result, "blocked")
        joined = "\n".join(lines)
        self.assertIn("pipeline state unreadable:", joined)

    def test_follow_verification_waits_for_report_before_completing(self):
        state_obj = new_state(self.task_dir.name)
        state_obj["state"] = "running"
        write_state_atomic(self.task_dir, state_obj)
        self.write_verification_run("unit_tests-run-a", "checking\n", with_metadata=True)

        def write_report_after_delay():
            time.sleep(0.3)
            (self.task_dir / "05_verification_report.md").write_text("# report\n", encoding="utf-8")

        thread = threading.Thread(target=write_report_after_delay)
        thread.start()
        lines = []
        result = tail.follow(self.task_dir, poll_interval=0.05, print_fn=lines.append, max_wait_seconds=5)
        thread.join()

        self.assertEqual(result, "complete")
        self.assertIn("verification complete", "\n".join(lines))

    def test_follow_times_out_if_run_never_completes(self):
        stdout_path = self.runs_dir.joinpath("04-pass-1-attempt-1-claude-run-a.stdout")
        stdout_path.write_text(
            CLAUDE_LINES[0] + "\n", encoding="utf-8"
        )
        self.write_running_sidecar(stdout_path)
        lines = []
        result = tail.follow(self.task_dir, poll_interval=0.05, print_fn=lines.append, max_wait_seconds=0.2)
        self.assertEqual(result, "timed_out")

    def test_follow_reports_missing_sidecar_as_corrupt(self):
        self.runs_dir.joinpath("04-pass-1-attempt-1-claude-orphan.stdout").write_text(
            CLAUDE_LINES[0] + "\n", encoding="utf-8"
        )
        lines = []
        result = tail.follow(self.task_dir, stage="04", poll_interval=0.01, print_fn=lines.append, max_wait_seconds=1)
        self.assertEqual(result, "orphaned")
        self.assertIn("metadata sidecar missing", "\n".join(lines))

    def test_follow_reports_malformed_sidecar_as_corrupt(self):
        stdout_path = self.runs_dir.joinpath("04-pass-1-attempt-1-claude-bad.stdout")
        stdout_path.write_text(CLAUDE_LINES[0] + "\n", encoding="utf-8")
        stdout_path.with_suffix(".json").write_text("not json", encoding="utf-8")
        lines = []
        result = tail.follow(self.task_dir, stage="04", poll_interval=0.01, print_fn=lines.append, max_wait_seconds=1)
        self.assertEqual(result, "corrupt_metadata")
        self.assertIn("metadata sidecar malformed", "\n".join(lines))

    def test_follow_reports_dead_pid_running_sidecar_as_orphaned(self):
        state_obj = new_state(self.task_dir.name)
        state_obj["state"] = "blocked"
        write_state_atomic(self.task_dir, state_obj)
        stdout_path = self.runs_dir.joinpath("05-pass-1-attempt-1-codex-stale.stdout")
        stdout_path.write_text(CLAUDE_LINES[0] + "\n", encoding="utf-8")
        self.write_running_sidecar(stdout_path, pid=self.dead_pid())
        lines = []

        result = tail.follow(self.task_dir, stage="05", poll_interval=0.01, print_fn=lines.append)

        self.assertEqual(result, "orphaned")
        joined = "\n".join(lines)
        self.assertIn("metadata sidecar stale", joined)
        self.assertIn("writer pid", joined)

    def test_brief_reports_missing_sidecar_as_orphaned_corrupt(self):
        self.write_run("04-pass-1-attempt-1-claude-orphan", CLAUDE_LINES, with_metadata=False)
        lines = []
        result = tail.brief(self.task_dir, print_fn=lines.append)
        self.assertEqual(result, "ok")
        self.assertIn("status: orphaned/corrupt (metadata sidecar missing", "\n".join(lines))

    def test_brief_reports_running_sidecar_as_in_progress(self):
        stdout_path = self.write_run("04-pass-1-attempt-1-claude-running", CLAUDE_LINES, with_metadata=False)
        self.write_running_sidecar(stdout_path)
        lines = []
        result = tail.brief(self.task_dir, print_fn=lines.append)
        self.assertEqual(result, "ok")
        self.assertIn("status: in progress", "\n".join(lines))

    def test_brief_reports_dead_pid_running_sidecar_as_orphaned_corrupt(self):
        stdout_path = self.write_run("04-pass-1-attempt-1-claude-stale", CLAUDE_LINES, with_metadata=False)
        self.write_running_sidecar(stdout_path, pid=self.dead_pid())
        lines = []
        result = tail.brief(self.task_dir, print_fn=lines.append)
        self.assertEqual(result, "ok")
        self.assertIn("status: orphaned/corrupt (metadata sidecar stale", "\n".join(lines))

    def test_follow_handles_no_runs(self):
        lines = []
        result = tail.follow(self.task_dir, print_fn=lines.append)
        self.assertEqual(result, "no_runs")


if __name__ == "__main__":
    unittest.main()
