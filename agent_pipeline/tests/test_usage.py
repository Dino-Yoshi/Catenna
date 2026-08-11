from __future__ import print_function

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agent_pipeline import usage


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger_path = Path(self.tmp.name) / "usage" / "ledger.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_and_read_roundtrip(self):
        entry = usage.build_entry("task-a", "run-1", "02", "codex", {"duration_seconds": 1.5, "exit_code": 0, "failure_class": None}, {"input_tokens": 10})
        self.assertTrue(usage.append_entry(self.ledger_path, entry))
        entries = usage.read_entries(self.ledger_path)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["task"], "task-a")
        self.assertEqual(entries[0]["usage"]["input_tokens"], 10)

    def test_build_entry_records_runtime_model_and_retry_metadata(self):
        entry = usage.build_entry(
            "task-a",
            "run-1",
            "04_gate",
            "claude",
            {
                "duration_seconds": 1.5,
                "exit_code": 0,
                "failure_class": None,
                "model": "claude-haiku-4-5",
                "pass_number": 2,
                "attempt_number": 3,
                "retry_reason": "max-turn completion retry",
            },
            {"input_tokens": 10},
        )

        self.assertEqual(entry["model"], "claude-haiku-4-5")
        self.assertEqual(entry["pass_number"], 2)
        self.assertEqual(entry["attempt_number"], 3)
        self.assertEqual(entry["retry_reason"], "max-turn completion retry")

    def test_read_missing_ledger_returns_empty(self):
        self.assertEqual(usage.read_entries(self.ledger_path), [])

    def test_read_skips_unparseable_lines(self):
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(self.ledger_path), "w", encoding="utf-8") as handle:
            handle.write("not json\n")
            handle.write(json.dumps({"agent": "codex", "stage": "02"}) + "\n")
            handle.write("\n")
        entries = usage.read_entries(self.ledger_path)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["agent"], "codex")

    def test_append_to_unwritable_dir_returns_false_not_raise(self):
        bad_path = Path("/proc/does-not-exist-usage/ledger.jsonl")
        self.assertFalse(usage.append_entry(bad_path, {"agent": "codex"}))

    def test_summarize_groups_by_agent(self):
        entries = [
            {"agent": "codex", "duration_seconds": 2.0, "failure_class": None, "usage": {"input_tokens": 10, "total_cost_usd": 0.01}},
            {"agent": "codex", "duration_seconds": 1.0, "failure_class": "timeout", "usage": None},
            {"agent": "claude", "duration_seconds": 3.0, "failure_class": None, "usage": {"output_tokens": 5}},
        ]
        summary = usage.summarize(entries, group_by="agent")
        codex = summary["groups"]["codex"]
        self.assertEqual(codex["count"], 2)
        self.assertEqual(codex["failures"], 1)
        self.assertEqual(codex["duration_seconds"], 3.0)
        self.assertEqual(codex["input_tokens"], 10)
        self.assertAlmostEqual(codex["total_cost_usd"], 0.01)
        self.assertEqual(summary["overall"]["count"], 3)
        self.assertEqual(summary["overall"]["output_tokens"], 5)

    def test_summarize_computes_cache_hit_ratio_when_tokens_known(self):
        entries = [
            {"agent": "codex", "usage": {"input_tokens": 30, "cache_read_tokens": 70}},
            {"agent": "claude", "usage": {"output_tokens": 5}},
        ]
        summary = usage.summarize(entries, group_by="agent")

        self.assertAlmostEqual(summary["groups"]["codex"]["cache_hit_ratio"], 0.7)
        self.assertIsNone(summary["groups"]["claude"]["cache_hit_ratio"])
        self.assertAlmostEqual(summary["overall"]["cache_hit_ratio"], 0.7)

    def test_cache_hit_ratio_unknown_for_cache_creation_only(self):
        summary = usage.summarize([{"agent": "codex", "usage": {"cache_creation_tokens": 10}}])

        self.assertTrue(summary["overall"]["tokens_known"])
        self.assertIsNone(summary["overall"]["cache_hit_ratio"])

    def test_concurrent_appends_from_separate_processes_all_land(self):
        script = (
            "import sys; sys.path.insert(0, %r)\n"
            "from agent_pipeline import usage\n"
            "for i in range(20):\n"
            "    usage.append_entry(%r, {'agent': 'codex', 'i': i, 'pid': %r})\n"
        )
        repo_root = str(Path(__file__).resolve().parents[2])
        procs = []
        for pid_marker in range(4):
            code = script % (repo_root, str(self.ledger_path), pid_marker)
            procs.append(subprocess.Popen([sys.executable, "-c", code]))
        for proc in procs:
            self.assertEqual(proc.wait(timeout=30), 0)
        entries = usage.read_entries(self.ledger_path)
        self.assertEqual(len(entries), 80)
        for entry in entries:
            self.assertIn("i", entry)


class CooldownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cooldowns_path = Path(self.tmp.name) / "usage" / "agent_cooldowns.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_missing_store_returns_empty(self):
        self.assertEqual(usage.load_cooldowns(self.cooldowns_path), {})

    def test_record_and_load_active_cooldown(self):
        self.assertTrue(usage.record_cooldown(self.cooldowns_path, "codex", "usage_limit", None, "task-a", "run-1", 900))
        active = usage.load_cooldowns(self.cooldowns_path)
        self.assertIn("codex", active)
        self.assertEqual(active["codex"]["reason"], "usage_limit")
        self.assertEqual(active["codex"]["source_task"], "task-a")

    def test_missing_reset_at_falls_back_to_default_seconds(self):
        before = time.time()
        usage.record_cooldown(self.cooldowns_path, "codex", "usage_limit", None, "task-a", "run-1", 60)
        active = usage.load_cooldowns(self.cooldowns_path)
        self.assertLess(active["codex"]["expires_at"], before + 61)
        self.assertGreater(active["codex"]["expires_at"], before + 55)

    def test_credible_reset_at_used_when_present(self):
        usage.record_cooldown(self.cooldowns_path, "codex", "rate_limit", "2099-01-01T00:00:00Z", "task-a", "run-1", 60)
        active = usage.load_cooldowns(self.cooldowns_path)
        self.assertGreater(active["codex"]["expires_at"], time.time() + 1000)

    def test_expired_cooldown_excluded_on_read(self):
        usage.record_cooldown(self.cooldowns_path, "codex", "usage_limit", "1970-01-01T00:00:01Z", "task-a", "run-1", 60)
        self.assertEqual(usage.load_cooldowns(self.cooldowns_path), {})

    def test_merge_is_extend_only_not_shorten(self):
        usage.record_cooldown(self.cooldowns_path, "codex", "usage_limit", "2099-01-01T00:00:00Z", "task-a", "run-1", 60)
        usage.record_cooldown(self.cooldowns_path, "codex", "usage_limit", None, "task-b", "run-2", 60)
        active = usage.load_cooldowns(self.cooldowns_path)
        self.assertGreater(active["codex"]["expires_at"], time.time() + 1000)

    def test_corrupt_store_degrades_to_empty(self):
        self.cooldowns_path.parent.mkdir(parents=True, exist_ok=True)
        self.cooldowns_path.write_text("not json", encoding="utf-8")
        self.assertEqual(usage.load_cooldowns(self.cooldowns_path), {})


if __name__ == "__main__":
    unittest.main()
