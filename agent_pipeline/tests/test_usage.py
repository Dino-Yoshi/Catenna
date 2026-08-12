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

    def test_build_outcome_entry_records_quality_fields(self):
        entry = usage.build_outcome_entry(
            "task-a",
            "run-1",
            "04",
            "claude",
            "claude-haiku-4-5",
            2,
            False,
            "gate_rejected",
        )

        self.assertEqual(entry["schema_version"], usage.SCHEMA_VERSION)
        self.assertEqual(entry["task"], "task-a")
        self.assertEqual(entry["run_id"], "run-1")
        self.assertEqual(entry["stage"], "04")
        self.assertEqual(entry["agent"], "claude")
        self.assertEqual(entry["model"], "claude-haiku-4-5")
        self.assertEqual(entry["pass_number"], 2)
        self.assertFalse(entry["accepted"])
        self.assertEqual(entry["classification"], "gate_rejected")
        self.assertIn("recorded_at", entry)

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

    def test_estimate_cost_usd_sums_configured_codex_rates(self):
        estimated = usage.estimate_cost_usd(
            {"input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 50, "cache_creation_tokens": 10},
            "gpt-5-codex",
            {"gpt-5-codex": {"input_tokens": 2, "output_tokens": 8, "cache_read_tokens": 0.5, "cache_creation_tokens": 1}},
        )

        self.assertAlmostEqual(estimated, ((100 * 2) + (20 * 8) + (50 * 0.5) + (10 * 1)) / 1000000.0)

    def test_estimate_cost_usd_unknown_when_model_or_table_missing(self):
        prices = {"gpt-5-codex": {"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 1, "cache_creation_tokens": 1}}
        self.assertIsNone(usage.estimate_cost_usd({"input_tokens": 1}, None, prices))
        self.assertIsNone(usage.estimate_cost_usd({"input_tokens": 1}, "missing", prices))
        self.assertIsNone(usage.estimate_cost_usd(None, "gpt-5-codex", prices))

    def test_estimate_cost_usd_missing_token_fields_count_as_zero(self):
        estimated = usage.estimate_cost_usd(
            {"input_tokens": 100, "output_tokens": None},
            "gpt-5-codex",
            {"gpt-5-codex": {"input_tokens": 2, "output_tokens": 8, "cache_read_tokens": 0.5, "cache_creation_tokens": 1}},
        )

        self.assertAlmostEqual(estimated, 200 / 1000000.0)

    def test_estimate_cost_usd_invalid_token_values_return_none(self):
        prices = {"gpt-5-codex": {"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 1, "cache_creation_tokens": 1}}
        self.assertIsNone(usage.estimate_cost_usd({"input_tokens": "many"}, "gpt-5-codex", prices))
        self.assertIsNone(usage.estimate_cost_usd({"input_tokens": True}, "gpt-5-codex", prices))

    def test_summarize_aggregates_estimated_cost_separately_from_real_cost(self):
        entries = [
            {"agent": "codex", "usage": {"input_tokens": 10, "total_cost_usd_estimated": 0.02}},
            {"agent": "codex", "usage": {"input_tokens": 5, "total_cost_usd": 0.01}},
            {"agent": "claude", "usage": {"total_cost_usd_estimated": None}},
        ]
        summary = usage.summarize(entries, group_by="agent")
        codex = summary["groups"]["codex"]

        self.assertAlmostEqual(codex["total_cost_usd_estimated"], 0.02)
        self.assertTrue(codex["cost_estimated_known"])
        self.assertAlmostEqual(codex["total_cost_usd"], 0.01)
        self.assertTrue(codex["cost_known"])
        self.assertAlmostEqual(summary["overall"]["total_cost_usd_estimated"], 0.02)

    def test_summarize_ignores_boolean_real_cost(self):
        summary = usage.summarize([{"agent": "codex", "usage": {"total_cost_usd": True}}])

        self.assertFalse(summary["overall"]["cost_known"])
        self.assertEqual(summary["overall"]["total_cost_usd"], 0.0)

    def test_summarize_historical_entries_without_estimated_cost_are_unknown(self):
        summary = usage.summarize([{"agent": "codex", "usage": {"input_tokens": 10, "total_cost_usd": 0.01}}])

        self.assertTrue(summary["overall"]["cost_known"])
        self.assertFalse(summary["overall"]["cost_estimated_known"])
        self.assertEqual(summary["overall"]["total_cost_usd_estimated"], 0.0)

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
