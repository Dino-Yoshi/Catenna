from __future__ import print_function

import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from agent_pipeline import controller
from agent_pipeline import usage
from agent_pipeline.failures import EXIT_BLOCKED, EXIT_SUCCESS, EXIT_VALIDATION, FAILURE_CLASS_MAX_TURNS, FAILURE_CLASS_SOURCE_FAILURE
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
        subprocess.check_call(["git", "init"], cwd=str(self.root), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.original_tasks_root = controller.TASKS_ROOT
        self.original_usage_root = controller.USAGE_ROOT
        self.original_repo_root = controller.REPO_ROOT
        self.original_load_config = controller.load_config
        self.original_run_verification = controller.verification.run_verification
        self.original_source_snapshot = controller.source_snapshot
        controller.TASKS_ROOT = self.root
        controller.USAGE_ROOT = self.root / "usage"
        controller.REPO_ROOT = self.root
        controller.load_config = lambda: self.config()
        controller.source_snapshot = lambda: ""
        # Real run_verification would shell out to a real `python3 -m unittest
        # discover`/`./gradlew` against the actual repo -- these tests fake
        # every agent CLI and must not depend on (or pay for) that. Default to
        # an "incomplete" report so auto_verified never triggers unless a test
        # explicitly overrides this to exercise that path.
        controller.verification.run_verification = lambda *args, **kwargs: self.verification_report()

    def tearDown(self):
        controller.TASKS_ROOT = self.original_tasks_root
        controller.USAGE_ROOT = self.original_usage_root
        controller.REPO_ROOT = self.original_repo_root
        controller.load_config = self.original_load_config
        controller.verification.run_verification = self.original_run_verification
        controller.source_snapshot = self.original_source_snapshot
        self.tmp.cleanup()

    def verification_report(self, overall_status="incomplete", coverage_status="no_data", driven_project_verified=None):
        report = {
            "schema_version": 1,
            "overall_status": overall_status,
            "checks": [{"name": "unit_tests", "status": "passed" if overall_status == "passed" else "not_attempted"}],
            "test_coverage_delta_signal": {"status": coverage_status},
        }
        if driven_project_verified is not None:
            report["driven_project_verified"] = driven_project_verified
        return report

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
            "verification": {"driven_project_commands": []},
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
                    sys.stderr.write("usage limit reached for this account\\n")
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
                elif "# Stage 4 - Final implementation brief" in prompt:
                    text = artifact("04")
                    stage = "04"
                elif "# Stage 4 - Final brief audit" in prompt:
                    text = artifact("04_gate")
                    stage = "04_gate"
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

    def stage_result(self, stage_key, output, failure_class=None, attempt_number=1, run_id="run-test"):
        runs = orchestrator_dir(self.task_dir) / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        prefix = "%s-pass-1-attempt-%s-codex-%s" % (stage_key, attempt_number, run_id)
        candidate = runs / (prefix + ".candidate.md")
        metadata = runs / (prefix + ".json")
        stdout = runs / (prefix + ".stdout")
        stderr = runs / (prefix + ".stderr")
        candidate.write_text(output, encoding="utf-8")
        metadata.write_text("{}\n", encoding="utf-8")
        stdout.write_text("", encoding="utf-8")
        stderr.write_text("", encoding="utf-8")
        return {
            "agent": "codex",
            "provider": "codex",
            "stage": stage_key,
            "execution_mode": "read-only",
            "exit_code": 1 if failure_class else 0,
            "failure_class": failure_class,
            "run_id": run_id,
            "pass_number": 1,
            "attempt_number": attempt_number,
            "attempt_kind": "normal",
            "retry_reason": "initial/no-retry",
            "candidate_artifact_path": str(candidate),
            "stdout_path": str(stdout),
            "stderr_path": str(stderr),
            "metadata_path": str(metadata),
            "_source_before": controller.source_snapshot(),
        }

    def run_stage_with_results(self, state, results, stage_key="02", force=False):
        calls = []
        original = controller.invoke_stage

        def fake_invoke(*args, **kwargs):
            calls.append((args, kwargs))
            return results.pop(0)

        try:
            controller.invoke_stage = fake_invoke
            code = controller.ensure_real_stage(self.task_dir, state, self.config(), stage_key, "read-only", {}, force=force)
        finally:
            controller.invoke_stage = original
        return code, calls

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

    def test_real_max_turn_unusable_requests_human_approved_retry(self):
        state = new_state(self.task, "run-test")
        output = "# Stage 2 - Technical specification\n\nPartial text without sections.\n"

        code, calls = self.run_stage_with_results(
            state,
            [self.stage_result("02", output, FAILURE_CLASS_MAX_TURNS, attempt_number=1)],
        )

        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(len(calls), 1)
        self.assertEqual(state["state"], "awaiting_retry_approval")
        self.assertEqual(state["attempts"]["02_human_approved_retry"], 1)
        self.assertEqual(state["pending_approval"]["retry_type"], "human_approved_full_stage_retry")
        self.assertEqual(state["pending_approval"]["failed_attempt_number"], 1)
        self.assertIn("failed_attempt_metadata_path", state["pending_approval"])

    def test_real_approval_resume_consumes_and_dispatches_owner_once(self):
        state = new_state(self.task, "run-test")
        initial = self.stage_result("02", "not useful\n", FAILURE_CLASS_MAX_TURNS, attempt_number=1)
        code, _calls = self.run_stage_with_results(state, [initial])
        self.assertEqual(code, EXIT_BLOCKED)
        write_state_atomic(self.task_dir, state)
        approval_id = state["pending_approval"]["approval_id"]

        self.assertEqual(controller.approve_retry(self.task, approval_id), EXIT_SUCCESS)
        approved = load_state(self.task_dir, self.task)
        self.assertEqual(approved["state"], "awaiting_retry_approval")

        success = self.stage_result("02", valid_artifact("02"), attempt_number=2)
        code, calls = self.run_stage_with_results(approved, [success])

        self.assertEqual(code, EXIT_SUCCESS)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(approved.get("pending_approval"))
        log_text = (orchestrator_dir(self.task_dir) / "log.jsonl").read_text(encoding="utf-8")
        self.assertIn("approval_granted", log_text)
        self.assertIn("approval_consumed", log_text)

    def test_real_post_approval_max_turn_blocks_without_second_approval_or_completion_retry(self):
        state = new_state(self.task, "run-test")
        initial = self.stage_result("02", "not useful\n", FAILURE_CLASS_MAX_TURNS, attempt_number=1)
        code, _calls = self.run_stage_with_results(state, [initial])
        self.assertEqual(code, EXIT_BLOCKED)
        approval_id = state["pending_approval"]["approval_id"]
        state["pending_approval"]["approved"] = True
        state["pending_approval"]["approved_at"] = "now"
        useful = "# Stage 2 - Technical specification\n\n## Summary\n\nPartial.\n"

        code, calls = self.run_stage_with_results(
            state,
            [self.stage_result("02", useful, FAILURE_CLASS_MAX_TURNS, attempt_number=2)],
        )

        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(len(calls), 1)
        self.assertEqual(state["state"], "blocked")
        self.assertEqual(state["pending_approval"]["approval_id"], approval_id)
        self.assertTrue(state["pending_approval"]["consumed"])
        self.assertNotIn("02_completion_retry", state["attempts"])

    def test_real_failed_completion_retry_requests_approval_with_audit_metadata(self):
        state = new_state(self.task, "run-test")
        useful = "# Stage 2 - Technical specification\n\n## Summary\n\nPartial.\n"
        completion = "# Stage 2 - Technical specification\n\n## Summary\n\nStill incomplete.\n"

        code, calls = self.run_stage_with_results(
            state,
            [
                self.stage_result("02", useful, FAILURE_CLASS_MAX_TURNS, attempt_number=1),
                self.stage_result("02", completion, None, attempt_number=2),
            ],
        )

        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(len(calls), 2)
        pending = state["pending_approval"]
        self.assertEqual(pending["retry_type"], "human_approved_full_stage_retry")
        self.assertEqual(pending["failed_attempt_number"], 1)
        self.assertEqual(pending["completion_retry_attempt_number"], 2)
        self.assertIn("failed_attempt_metadata_path", pending)
        self.assertIn("completion_retry_metadata_path", pending)

    def test_completion_retry_preserves_extra_context(self):
        state = new_state(self.task, "run-test")
        useful = "# Stage 2 - Technical specification\n\n## Summary\n\nPartial.\n"
        completion = valid_artifact("02")
        calls = []
        original = controller.invoke_stage
        results = [
            self.stage_result("02", useful, FAILURE_CLASS_MAX_TURNS, attempt_number=1),
            self.stage_result("02", completion, None, attempt_number=2),
        ]

        def fake_invoke(*args, **kwargs):
            calls.append((args, kwargs))
            return results.pop(0)

        try:
            controller.invoke_stage = fake_invoke
            code = controller.ensure_real_stage(
                self.task_dir,
                state,
                self.config(),
                "02",
                "read-only",
                {},
                extra_context="gate feedback",
            )
        finally:
            controller.invoke_stage = original

        self.assertEqual(code, EXIT_SUCCESS)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1]["extra_context"], "gate feedback")
        self.assertEqual(calls[1][1]["extra_context"], "gate feedback")
        self.assertEqual(calls[1][1]["completion_for"], useful)

    def test_real_non_owner_completed_stage_short_circuits_while_later_approval_pending(self):
        state = new_state(self.task, "run-test")
        state["state"] = "awaiting_retry_approval"
        state["completed_stages"] = ["00", "01", "02"]
        state["pending_approval"] = {
            "approval_id": "retry-later",
            "stage": "03",
            "approved": False,
            "consumed": False,
        }

        code, calls = self.run_stage_with_results(state, [], stage_key="02")

        self.assertEqual(code, EXIT_SUCCESS)
        self.assertEqual(calls, [])
        self.assertEqual(state["pending_approval"]["stage"], "03")

    def test_real_owner_completed_approved_pending_approval_is_consumed_and_cleared(self):
        state = new_state(self.task, "run-test")
        state["state"] = "awaiting_retry_approval"
        state["completed_stages"] = ["00", "01", "02"]
        state["pending_approval"] = {
            "approval_id": "retry-owner",
            "stage": "02",
            "approved": True,
            "consumed": False,
        }

        code, calls = self.run_stage_with_results(state, [], stage_key="02")

        self.assertEqual(code, EXIT_SUCCESS)
        self.assertEqual(calls, [])
        self.assertIsNone(state.get("pending_approval"))
        log_text = (orchestrator_dir(self.task_dir) / "log.jsonl").read_text(encoding="utf-8")
        self.assertIn("approval_consumed", log_text)

    def test_real_owner_completed_unapproved_pending_approval_blocks_without_clearing(self):
        state = new_state(self.task, "run-test")
        state["state"] = "awaiting_retry_approval"
        state["completed_stages"] = ["00", "01", "02"]
        state["pending_approval"] = {
            "approval_id": "retry-owner",
            "stage": "02",
            "approved": False,
            "consumed": False,
        }

        code, calls = self.run_stage_with_results(state, [], stage_key="02")

        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(calls, [])
        self.assertEqual(state["pending_approval"]["approval_id"], "retry-owner")

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

    def test_stage6_auto_verification_passes_configured_toggles(self):
        cfg = self.config()
        cfg["verification"] = {"driven_project_commands": [], "skip_self_check": True, "build_implies_compile": True}
        controller.load_config = lambda: cfg
        seen = {}

        def fake_run_verification(*args, **kwargs):
            seen.update(kwargs)
            return self.verification_report()

        controller.verification.run_verification = fake_run_verification

        code = controller.pipeline_run(self.task, allow_dirty=True)

        self.assertEqual(code, EXIT_BLOCKED)
        self.assertTrue(seen["skip_self_check"])
        self.assertTrue(seen["build_implies_compile"])

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

    def test_stage5_dirty_source_block_names_allow_dirty_flag(self):
        original_git_status = controller.git_status
        original_ensure = controller.ensure_real_stage
        controller.git_status = lambda root: " M source.py\n"

        def ensure_without_stage5(*args, **kwargs):
            if args[3] == "05":
                raise AssertionError("Stage 5 should not run after dirty source block")
            return original_ensure(*args, **kwargs)

        controller.ensure_real_stage = ensure_without_stage5
        self.addCleanup(lambda: setattr(controller, "git_status", original_git_status))
        self.addCleanup(lambda: setattr(controller, "ensure_real_stage", original_ensure))

        code = controller.pipeline_run(self.task, allow_dirty=False)

        self.assertEqual(code, EXIT_BLOCKED)
        state = load_state(self.task_dir, self.task)
        reason = state["last_failure"]["reason"]
        self.assertIn("--allow-dirty", reason)
        self.assertNotIn("ALLOW_DIRTY=1", reason)
        self.assertEqual(state["last_failure"]["failure_class"], FAILURE_CLASS_SOURCE_FAILURE)

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
        controller.verification.run_verification = lambda *a, **k: self.verification_report(overall_status="failed", driven_project_verified=True)
        code = controller.pipeline_run(self.task, allow_dirty=True)
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(load_state(self.task_dir, self.task)["state"], "awaiting_human_test")

        self.setUp_for_second_task()
        controller.verification.run_verification = lambda *a, **k: self.verification_report(overall_status="passed", coverage_status="flagged", driven_project_verified=True)
        code = controller.pipeline_run(self.task, allow_dirty=True)
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(load_state(self.task_dir, self.task)["state"], "awaiting_human_test")

        self.setUp_for_second_task("real-fixture-3")
        controller.verification.run_verification = lambda *a, **k: self.verification_report(overall_status="passed", coverage_status="ok")
        code = controller.pipeline_run(self.task, allow_dirty=True)
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(load_state(self.task_dir, self.task)["state"], "awaiting_human_test")

    def setUp_for_second_task(self, task="real-fixture-2"):
        # Reset just enough state to run pipeline_run again from scratch
        # against a fresh task directory within the same test method.
        self.task = task
        self.task_dir = self.root / self.task
        self.task_dir.mkdir(parents=True)
        (self.task_dir / CONTRACTS["00"].filename).write_text(valid_artifact("00"), encoding="utf-8")
        (self.task_dir / CONTRACTS["01"].filename).write_text(valid_artifact("01"), encoding="utf-8")

    def test_auto_verified_path_drives_stage_06_through_08_in_one_call(self):
        controller.verification.run_verification = lambda *a, **k: self.verification_report(
            overall_status="passed",
            coverage_status="ok",
            driven_project_verified=True,
        )

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

    def test_enable_auto_verified_false_keeps_manual_stage6_checkpoint(self):
        config = self.config()
        config["enable_auto_verified"] = False
        controller.load_config = lambda: config
        controller.verification.run_verification = lambda *a, **k: self.verification_report(
            overall_status="passed",
            coverage_status="ok",
            driven_project_verified=True,
        )

        code = controller.pipeline_run(self.task, allow_dirty=True)

        self.assertEqual(code, EXIT_BLOCKED)
        state = load_state(self.task_dir, self.task)
        self.assertEqual(state["state"], "awaiting_human_test")
        self.assertFalse((self.task_dir / CONTRACTS["06"].filename).exists())
        handoff = json.loads((self.task_dir / "05_supervisor_handoff.json").read_text(encoding="utf-8"))
        self.assertEqual(handoff["route"], "manual_test")

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

    def test_manual_stage6_resume_acknowledges_stage05_input_before_stage08(self):
        code = controller.pipeline_run(self.task, allow_dirty=True)
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(load_state(self.task_dir, self.task)["state"], "awaiting_human_test")

        (self.task_dir / CONTRACTS["06"].filename).write_text(
            "# Stage 6 - Manual test notes\n\n## Decision\n\n- [x] Accept\n- [ ] Reject\n- [ ] Needs follow-up\n",
            encoding="utf-8",
        )

        captured = {}
        original_ensure_stage08_decision = controller.ensure_stage08_decision

        def spy(task_dir, state):
            captured["input_hashes"] = dict(state.get("input_hashes") or {})
            return original_ensure_stage08_decision(task_dir, state)

        controller.ensure_stage08_decision = spy
        self.addCleanup(lambda: setattr(controller, "ensure_stage08_decision", original_ensure_stage08_decision))

        code = controller.pipeline_run(self.task, allow_dirty=True)

        self.assertEqual(code, EXIT_SUCCESS)
        self.assertIn(CONTRACTS["05"].filename, captured["input_hashes"])
        self.assertEqual(
            captured["input_hashes"][CONTRACTS["05"].filename],
            controller.sha256_file(self.task_dir / CONTRACTS["05"].filename),
        )

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
        controller.verification.run_verification = lambda *a, **k: self.verification_report(
            overall_status="passed",
            coverage_status="ok",
            driven_project_verified=True,
        )
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
