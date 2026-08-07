from __future__ import print_function

import json
import os
import tempfile
import textwrap
import unittest
from pathlib import Path

from agent_pipeline import controller
from agent_pipeline import usage
from agent_pipeline.failures import EXIT_BLOCKED, EXIT_SUCCESS, EXIT_VALIDATION
from agent_pipeline.mock_agent import valid_artifact
from agent_pipeline.state import CONTRACTS, load_state, new_state, orchestrator_dir, write_state_atomic


class RealPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.task = "real-fixture"
        self.task_dir = self.root / self.task
        self.task_dir.mkdir(parents=True)
        (self.task_dir / CONTRACTS["00"].filename).write_text(valid_artifact("00"), encoding="utf-8")
        (self.task_dir / CONTRACTS["01"].filename).write_text(valid_artifact("01"), encoding="utf-8")
        self.fake = self.write_fake_agent()
        self.original_tasks_root = controller.TASKS_ROOT
        self.original_usage_root = controller.USAGE_ROOT
        self.original_load_config = controller.load_config
        self.original_run_verification = controller.verification.run_verification
        controller.TASKS_ROOT = self.root
        controller.USAGE_ROOT = self.root / "usage"
        controller.load_config = lambda: self.config()
        # Real run_verification would shell out to a real `python3 -m unittest
        # discover`/`./gradlew` against the actual repo -- these tests fake
        # every agent CLI and must not depend on (or pay for) that. Default to
        # an "incomplete" report so auto_verified never triggers unless a test
        # explicitly overrides this to exercise that path.
        controller.verification.run_verification = lambda *args, **kwargs: self.verification_report()

    def tearDown(self):
        controller.TASKS_ROOT = self.original_tasks_root
        controller.USAGE_ROOT = self.original_usage_root
        controller.load_config = self.original_load_config
        controller.verification.run_verification = self.original_run_verification
        self.tmp.cleanup()

    def verification_report(self, overall_status="incomplete", coverage_status="no_data"):
        return {
            "schema_version": 1,
            "overall_status": overall_status,
            "checks": [{"name": "unit_tests", "status": "passed" if overall_status == "passed" else "not_attempted"}],
            "test_coverage_delta_signal": {"status": coverage_status},
        }

    def config(self, gate_ready=True, max_gate_passes=2):
        return {
            "schema_version": 2,
            "default_safety_mode": "strict",
            "supported_safety_modes": ["strict", "continuity"],
            "stage_attempt_budget": 1,
            "max_gate_passes": max_gate_passes,
            "timeout_seconds": 30,
            "allow_degraded_same_agent_review": False,
            "enable_auto_verified": True,
            "roles": {
                "02": {"primary": "codex", "fallbacks": []},
                "03": {"primary": "codex", "fallbacks": []},
                "04": {"primary": "codex", "fallbacks": []},
                "04_gate": {"primary": "claude", "fallbacks": [], "independent_from": "04"},
                "05": {"primary": "codex", "fallbacks": []},
                "07": {"primary": "claude", "fallbacks": [], "independent_from": "05"},
                "overseer": {"primary": "codex", "fallbacks": []},
            },
            "agents": {
                "codex": {"command": str(self.fake), "model": "fake", "read_args": [], "write_args": [], "workspace_write": True, "enabled": True},
                "claude": {"command": str(self.fake), "model": "fake", "read_effort": "low", "write_effort": "low", "read_args": [], "write_args": [], "workspace_write": False, "enabled": True},
                "agy": {"command": str(self.fake), "model": "fake", "common_args": [], "read_args": [], "write_args": [], "prompt_mode": "print", "stdin_mode_allowed": False, "workspace_write": False, "enabled": False},
            },
            "turn_budgets": {"02": 5, "03": 5, "04": 5, "04_gate": 5, "05": 5, "07": 5, "overseer": 5},
            "gate_ready": gate_ready,
        }

    def write_fake_agent(self):
        path = self.root / "fake_agent.py"
        path.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import os
                import sys

                def artifact(stage):
                    headings = {
                        "02": ("# Stage 2 - Technical specification", ["Summary", "Source request", "Must-have requirements", "Nice-to-have requirements", "Non-goals", "Affected systems", "Proposed implementation shape", "Data/config/API changes", "Compatibility constraints", "Risks and edge cases", "Acceptance criteria", "Verification plan", "Open questions"]),
                        "03": ("# Stage 3 - Specification audit", ["Summary", "Blocking issues", "Nonblocking issues", "Compatibility risks", "Architecture concerns", "Implementation traps", "Required revision targets", "YAML gate"]),
                        "04": ("# Stage 4 - Final implementation brief", ["Implementation objective", "Required behavior", "Explicit non-goals", "Files/classes likely involved", "Implementation constraints", "Edge cases to handle"]),
                        "04_gate": ("# Stage 4 - Final brief audit", ["Summary", "Blocking issues", "Nonblocking issues", "Implementation risks", "Required brief revisions", "YAML gate"]),
                        "05": ("# Stage 5 - Implementation report", ["Summary of changes", "Files changed", "Behavior implemented", "Verification performed", "Build/test results", "Deviations from brief", "Known limitations", "Follow-up recommendations"]),
                        "07": ("# Stage 7 - Diff review", ["Summary", "Correctness findings", "Maintainability findings", "Regression risks", "Performance risks", "Brief compliance", "Required fixes", "Recommended follow-ups", "Verdict"]),
                    }
                    heading, sections = headings[stage]
                    out = [heading, ""]
                    for section in sections:
                        out += ["## " + section, ""]
                        if section == "YAML gate":
                            ready = "false" if os.environ.get("FAKE_GATE_REJECT") == "1" and stage == "04_gate" else "true"
                            out += ["```yaml", "ready_for_implementation: " + ready, "blocking_issues: []", "nonblocking_issues: []", "required_revision_targets: []", "```"]
                        else:
                            out += ["Fake content."]
                        out += [""]
                    if stage == "07":
                        out += [os.environ.get("FAKE_STAGE7_VERDICT", "accept")]
                    return "\\n".join(out).rstrip() + "\\n"

                if os.environ.get("FAKE_USAGE_LIMIT_ON_SANDBOX") == "1" and "--sandbox" in sys.argv:
                    sys.stdout.write("usage limit reached for this account\\n")
                    sys.exit(1)

                prompt = ""
                if sys.argv and sys.argv[1:2] == ["exec"]:
                    prompt = sys.stdin.read()
                elif sys.argv and "-p" in sys.argv:
                    prompt = sys.argv[-1]
                output_path = None
                if "--output-last-message" in sys.argv:
                    output_path = sys.argv[sys.argv.index("--output-last-message") + 1]
                if "Implementation handoff overseer" in prompt:
                    text = json.dumps({"route": "manual_test", "summary": ["fake"], "verified": [], "needs_human_testing": ["manual"], "known_limitations": [], "next_action": "Record Stage 6 notes."}) + "\\n"
                elif "# Stage 7 - Diff review" in prompt:
                    text = artifact("07")
                    stage = "07"
                elif "# Stage 5 - Implementation report" in prompt:
                    text = artifact("05")
                    stage = "05"
                elif "# Stage 4 - Final brief audit" in prompt:
                    text = artifact("04_gate")
                    stage = "04_gate"
                elif "# Stage 4 - Final implementation brief" in prompt:
                    text = artifact("04")
                    stage = "04"
                elif "# Stage 3 - Specification audit" in prompt:
                    text = artifact("03")
                    stage = "03"
                else:
                    text = artifact("02")
                    stage = "02"
                count_path = os.environ.get("FAKE_COUNT_PATH")
                if count_path and "stage" in locals():
                    with open(count_path, "a") as handle:
                        handle.write(stage + "\\n")
                if output_path:
                    with open(output_path, "w") as handle:
                        handle.write(text)
                else:
                    sys.stdout.write(text)
                """
            ),
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def test_real_pipeline_completes_to_human_checkpoint(self):
        code = controller.pipeline_run(self.task, allow_dirty=True)

        self.assertEqual(code, EXIT_BLOCKED)
        state = load_state(self.task_dir, self.task)
        self.assertEqual(state["state"], "awaiting_human_test")
        self.assertEqual(state["current_stage"], "06")
        self.assertTrue((self.task_dir / "05_implementation_manifest.json").exists())
        self.assertTrue((self.task_dir / "05_supervisor_handoff.json").exists())
        self.assertFalse((self.task_dir / CONTRACTS["06"].filename).exists())
        self.assertFalse((self.task_dir / CONTRACTS["07"].filename).exists())
        self.assertFalse((self.task_dir / CONTRACTS["08"].filename).exists())

    def test_rejected_stage4_gate_stops_before_stage5(self):
        os.environ["FAKE_GATE_REJECT"] = "1"
        self.addCleanup(lambda: os.environ.pop("FAKE_GATE_REJECT", None))
        controller.load_config = lambda: self.config(max_gate_passes=1)

        code = controller.pipeline_run(self.task, allow_dirty=True)

        self.assertEqual(code, EXIT_BLOCKED)
        state = load_state(self.task_dir, self.task)
        self.assertEqual(state["state"], "blocked")
        self.assertEqual(state["last_failure"]["stage"], "04_gate")
        self.assertEqual(state["last_failure"]["failure_class"], "gate_pass_limit_exhausted")
        self.assertEqual(state["completed_stages"], ["00", "01", "02", "03", "04"])
        self.assertFalse((self.task_dir / CONTRACTS["05"].filename).exists())

    def test_identical_stage4_revision_blocks_as_gate_rejected(self):
        os.environ["FAKE_GATE_REJECT"] = "1"
        self.addCleanup(lambda: os.environ.pop("FAKE_GATE_REJECT", None))
        controller.load_config = lambda: self.config(max_gate_passes=2)

        code = controller.pipeline_run(self.task, allow_dirty=True)

        self.assertEqual(code, EXIT_BLOCKED)
        state = load_state(self.task_dir, self.task)
        self.assertEqual(state["state"], "blocked")
        self.assertEqual(state["last_failure"]["stage"], "04_gate")
        self.assertEqual(state["last_failure"]["failure_class"], "gate_rejected")
        self.assertEqual(state["current_stage"], "04_gate")
        self.assertEqual(state["completed_stages"], ["00", "01", "02", "03", "04"])
        self.assertFalse((self.task_dir / CONTRACTS["05"].filename).exists())

    def test_strict_mode_blocks_without_independent_stage4_reviewer(self):
        config = self.config()
        config["roles"]["04_gate"] = {"primary": "codex", "fallbacks": [], "independent_from": "04"}
        controller.load_config = lambda: config

        code = controller.pipeline_run(self.task, allow_dirty=True)

        self.assertEqual(code, EXIT_BLOCKED)
        state = load_state(self.task_dir, self.task)
        self.assertEqual(state["state"], "blocked")
        self.assertEqual(state["last_failure"]["stage"], "04_gate")

    def test_rerun_skips_completed_real_stages(self):
        count_path = self.root / "counts.txt"
        os.environ["FAKE_COUNT_PATH"] = str(count_path)
        self.addCleanup(lambda: os.environ.pop("FAKE_COUNT_PATH", None))

        controller.pipeline_run(self.task, allow_dirty=True)
        first = count_path.read_text(encoding="utf-8").splitlines()
        controller.pipeline_run(self.task, allow_dirty=True)
        second = count_path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(first.count("02"), second.count("02"))
        self.assertEqual(first.count("03"), second.count("03"))
        self.assertEqual(first.count("04"), second.count("04"))
        self.assertEqual(first.count("04_gate"), second.count("04_gate"))
        self.assertEqual(first.count("05"), second.count("05"))

    def test_unchanged_human_checkpoint_rerun_is_noop(self):
        count_path = self.root / "counts.txt"
        os.environ["FAKE_COUNT_PATH"] = str(count_path)
        self.addCleanup(lambda: os.environ.pop("FAKE_COUNT_PATH", None))

        controller.pipeline_run(self.task, allow_dirty=True)
        state_file = orchestrator_dir(self.task_dir) / "state.json"
        log_file = orchestrator_dir(self.task_dir) / "log.jsonl"
        watched = [
            state_file,
            log_file,
            self.task_dir / "05_implementation_manifest.json",
            self.task_dir / "05_supervisor_handoff.json",
            self.task_dir / "05_supervisor_handoff.md",
            self.task_dir / "handoff.md",
        ]
        before_text = {str(path): path.read_text(encoding="utf-8") for path in watched}
        before_counts = count_path.read_text(encoding="utf-8")

        code = controller.pipeline_run(self.task, allow_dirty=True)

        self.assertEqual(code, EXIT_BLOCKED)
        after_text = {str(path): path.read_text(encoding="utf-8") for path in watched}
        self.assertEqual(before_text, after_text)
        self.assertEqual(before_counts, count_path.read_text(encoding="utf-8"))

    def test_valid_stage5_report_without_provenance_blocks(self):
        for stage_key in ("02", "03", "04", "04_gate", "05"):
            (self.task_dir / CONTRACTS[stage_key].filename).write_text(valid_artifact(stage_key), encoding="utf-8")

        code = controller.pipeline_run(self.task, allow_dirty=True)

        self.assertEqual(code, EXIT_BLOCKED)
        state = load_state(self.task_dir, self.task)
        self.assertEqual(state["state"], "blocked")
        self.assertEqual(state["last_failure"]["stage"], "05")
        self.assertEqual(state["last_failure"]["failure_class"], "stage5_ambiguity")
        self.assertIn("no matching successful real Stage 5 provenance", state["last_failure"]["reason"])

    def test_valid_stage5_report_with_partial_postprocessing_blocks(self):
        for stage_key in ("02", "03", "04", "04_gate", "05"):
            (self.task_dir / CONTRACTS[stage_key].filename).write_text(valid_artifact(stage_key), encoding="utf-8")
        runs = orchestrator_dir(self.task_dir) / "runs"
        runs.mkdir(parents=True)
        candidate = runs / "05-pass-1-attempt-1-codex-oldrun.candidate.md"
        candidate.write_text(valid_artifact("05"), encoding="utf-8")
        stdout = runs / "05-pass-1-attempt-1-codex-oldrun.stdout"
        stderr = runs / "05-pass-1-attempt-1-codex-oldrun.stderr"
        metadata = runs / "05-pass-1-attempt-1-codex-oldrun.json"
        stdout.write_text("", encoding="utf-8")
        stderr.write_text("", encoding="utf-8")
        metadata.write_text("{}\n", encoding="utf-8")
        state = new_state(self.task, "oldrun")
        state["dirty_baseline"] = {"captured_at": "now", "entries": [], "hashes": {}}
        state["real_stage_runs"] = {
            "05": [
                {
                    "agent": "codex",
                    "provider": "codex",
                    "stage": "05",
                    "execution_mode": "workspace-write",
                    "exit_code": 0,
                    "failure_class": None,
                    "run_id": "oldrun",
                    "pass_number": 1,
                    "attempt_number": 1,
                    "attempt_kind": "normal",
                    "retry_reason": "initial/no-retry",
                    "candidate_artifact_path": str(candidate),
                    "stdout_path": str(stdout),
                    "stderr_path": str(stderr),
                    "metadata_path": str(metadata),
                }
            ]
        }
        (self.task_dir / "05_implementation_manifest.json").write_text('{"stage": "05", "verification": {}, "changed_files": []}\n', encoding="utf-8")
        write_state_atomic(self.task_dir, state)

        code = controller.pipeline_run(self.task, allow_dirty=True)

        self.assertEqual(code, EXIT_BLOCKED)
        loaded = load_state(self.task_dir, self.task)
        self.assertEqual(loaded["state"], "blocked")
        self.assertEqual(loaded["last_failure"]["stage"], "05")
        self.assertEqual(loaded["last_failure"]["failure_class"], "stage5_ambiguity")

    def test_real_attempt_artifacts_include_unique_attempt_identity(self):
        controller.pipeline_run(self.task, allow_dirty=True)

        runs = sorted((orchestrator_dir(self.task_dir) / "runs").iterdir())
        names = [path.name for path in runs if path.suffix in (".json", ".stdout", ".stderr") or path.name.endswith(".candidate.md")]
        self.assertTrue(any(name.startswith("05-pass-1-attempt-1-codex-") and name.endswith(".candidate.md") for name in names))
        self.assertTrue(any(name.startswith("05-pass-1-attempt-1-codex-") and name.endswith(".stdout") for name in names))
        self.assertTrue(any(name.startswith("05-pass-1-attempt-1-codex-") and name.endswith(".stderr") for name in names))
        self.assertTrue(any(name.startswith("05-pass-1-attempt-1-codex-") and name.endswith(".json") for name in names))

    def test_weak_evidence_falls_back_to_awaiting_human_test_unchanged(self):
        # Default self.verification_report() is "incomplete" -- confirms the
        # pre-Phase-3 behavior is preserved byte-for-byte when evidence isn't
        # strong enough for auto_verified.
        code = controller.pipeline_run(self.task, allow_dirty=True)

        self.assertEqual(code, EXIT_BLOCKED)
        state = load_state(self.task_dir, self.task)
        self.assertEqual(state["state"], "awaiting_human_test")
        self.assertEqual(state["current_stage"], "06")
        self.assertFalse((self.task_dir / CONTRACTS["06"].filename).exists())

    def test_failed_or_flagged_evidence_does_not_trigger_auto_verified(self):
        controller.verification.run_verification = lambda *a, **k: self.verification_report(overall_status="failed")
        code = controller.pipeline_run(self.task, allow_dirty=True)
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(load_state(self.task_dir, self.task)["state"], "awaiting_human_test")

        self.setUp_for_second_task()
        controller.verification.run_verification = lambda *a, **k: self.verification_report(overall_status="passed", coverage_status="flagged")
        code = controller.pipeline_run(self.task, allow_dirty=True)
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(load_state(self.task_dir, self.task)["state"], "awaiting_human_test")

    def setUp_for_second_task(self):
        # Reset just enough state to run pipeline_run again from scratch
        # against a fresh task directory within the same test method.
        self.task = "real-fixture-2"
        self.task_dir = self.root / self.task
        self.task_dir.mkdir(parents=True)
        (self.task_dir / CONTRACTS["00"].filename).write_text(valid_artifact("00"), encoding="utf-8")
        (self.task_dir / CONTRACTS["01"].filename).write_text(valid_artifact("01"), encoding="utf-8")

    def test_auto_verified_path_drives_stage_06_through_08_in_one_call(self):
        controller.verification.run_verification = lambda *a, **k: self.verification_report(overall_status="passed", coverage_status="ok")

        code = controller.pipeline_run(self.task, allow_dirty=True)

        self.assertEqual(code, EXIT_SUCCESS)
        state = load_state(self.task_dir, self.task)
        self.assertEqual(state["state"], "complete")
        self.assertEqual(state["completed_stages"], ["00", "01", "02", "03", "04", "04_gate", "05", "06", "07", "08"])

        stage06 = (self.task_dir / CONTRACTS["06"].filename).read_text(encoding="utf-8")
        self.assertIn("completed automatically", stage06)
        self.assertIn("- [x] Accept", stage06)
        stage08 = (self.task_dir / CONTRACTS["08"].filename).read_text(encoding="utf-8")
        self.assertIn("- [x] Accept", stage08)

        handoff = json.loads((self.task_dir / "05_supervisor_handoff.json").read_text(encoding="utf-8"))
        self.assertEqual(handoff["route"], "auto_verified")

    def test_manual_stage6_completion_drives_stage_07_08_on_resume(self):
        code = controller.pipeline_run(self.task, allow_dirty=True)
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(load_state(self.task_dir, self.task)["state"], "awaiting_human_test")

        (self.task_dir / CONTRACTS["06"].filename).write_text(
            "# Stage 6 - Manual test notes\n\n## Decision\n\n- [x] Accept\n- [ ] Reject\n- [ ] Needs follow-up\n",
            encoding="utf-8",
        )

        code = controller.pipeline_run(self.task, allow_dirty=True)

        self.assertEqual(code, EXIT_SUCCESS)
        state = load_state(self.task_dir, self.task)
        self.assertEqual(state["state"], "complete")
        self.assertEqual(state["completed_stages"], ["00", "01", "02", "03", "04", "04_gate", "05", "06", "07", "08"])
        stage08 = (self.task_dir / CONTRACTS["08"].filename).read_text(encoding="utf-8")
        self.assertIn("- [x] Accept", stage08)

    def test_needs_followup_stage7_verdict_yields_combined_needs_followup(self):
        (self.task_dir / CONTRACTS["06"].filename).write_text(
            "# Stage 6 - Manual test notes\n\n## Decision\n\n- [x] Accept\n- [ ] Reject\n- [ ] Needs follow-up\n",
            encoding="utf-8",
        )
        os.environ["FAKE_STAGE7_VERDICT"] = "needs_followup"
        self.addCleanup(lambda: os.environ.pop("FAKE_STAGE7_VERDICT", None))

        controller.pipeline_run(self.task, allow_dirty=True)  # first call: stops at awaiting_human_test
        code = controller.pipeline_run(self.task, allow_dirty=True)

        self.assertEqual(code, EXIT_VALIDATION)
        state = load_state(self.task_dir, self.task)
        self.assertEqual(state["state"], "complete")
        stage08 = (self.task_dir / CONTRACTS["08"].filename).read_text(encoding="utf-8")
        self.assertIn("- [x] Needs follow-up", stage08)
        self.assertIn("diff review verdict (needs_followup)", stage08)

    def test_resumed_pipeline_run_on_complete_task_is_a_pure_noop(self):
        controller.verification.run_verification = lambda *a, **k: self.verification_report(overall_status="passed", coverage_status="ok")
        controller.pipeline_run(self.task, allow_dirty=True)
        count_path = self.root / "counts.txt"
        os.environ["FAKE_COUNT_PATH"] = str(count_path)
        self.addCleanup(lambda: os.environ.pop("FAKE_COUNT_PATH", None))
        count_path.write_text("", encoding="utf-8")

        code = controller.pipeline_run(self.task, allow_dirty=True)

        self.assertEqual(code, EXIT_SUCCESS)
        # No stage dispatch at all on a fully-complete resumed run.
        self.assertEqual(count_path.read_text(encoding="utf-8"), "")

    def test_usage_ledger_enabled_by_default_writes_entries(self):
        controller.pipeline_run(self.task, allow_dirty=True)

        entries = usage.read_entries(controller.usage_ledger_path())
        self.assertGreater(len(entries), 0)
        self.assertTrue(any(entry.get("stage") == "02" for entry in entries))

    def test_usage_ledger_disabled_writes_nothing(self):
        config = self.config()
        config["usage_ledger"] = {"enabled": False}
        controller.load_config = lambda: config

        controller.pipeline_run(self.task, allow_dirty=True)

        self.assertFalse(controller.usage_ledger_path().exists())

    def test_cross_task_cooldowns_disabled_records_nothing(self):
        config = self.config()
        config["cross_task_cooldowns"] = {"enabled": False, "default_cooldown_seconds": 900}
        controller.load_config = lambda: config
        os.environ["FAKE_USAGE_LIMIT_ON_SANDBOX"] = "1"
        self.addCleanup(lambda: os.environ.pop("FAKE_USAGE_LIMIT_ON_SANDBOX", None))

        controller.pipeline_run(self.task, allow_dirty=True)

        self.assertFalse(controller.cooldown_store_path().exists())

    def test_usage_limit_records_cross_task_cooldown_and_reorders_fallback(self):
        # First task: codex hits a usage_limit failure on Stage 02 and falls
        # back to claude within its own run -- this also records a
        # cross-task cooldown for codex.
        config = self.config()
        config["stage_attempt_budget"] = 2
        config["roles"]["02"] = {"primary": "codex", "fallbacks": ["claude"]}
        controller.load_config = lambda: config
        os.environ["FAKE_USAGE_LIMIT_ON_SANDBOX"] = "1"
        self.addCleanup(lambda: os.environ.pop("FAKE_USAGE_LIMIT_ON_SANDBOX", None))

        controller.pipeline_run(self.task, allow_dirty=True)

        cooldowns = usage.load_cooldowns(controller.cooldown_store_path())
        self.assertIn("codex", cooldowns)

        # Second, fresh task: codex would succeed if tried (the env-gated
        # failure is cleared below), but the cross-task cooldown recorded
        # above should still deprioritize it below claude for Stage 02 --
        # proving the reordering, not a second in-run failure, is what
        # picks claude.
        os.environ.pop("FAKE_USAGE_LIMIT_ON_SANDBOX", None)
        self.setUp_for_second_task()
        controller.pipeline_run(self.task, allow_dirty=True)

        state = load_state(self.task_dir, self.task)
        self.assertEqual(state.get("stage_agents", {}).get("02"), "claude")
        self.assertEqual(state.get("run_unavailable_agents"), {})


if __name__ == "__main__":
    unittest.main()
