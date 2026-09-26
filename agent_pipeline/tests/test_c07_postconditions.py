"""C07 regressions: uniform read-only and protected-record postconditions."""

from __future__ import print_function

import json
import unittest
from pathlib import Path

from agent_pipeline import controller
from agent_pipeline.failures import EXIT_BLOCKED, EXIT_SUCCESS, FAILURE_CLASS_MAX_TURNS
from agent_pipeline.mock_agent import valid_artifact
from agent_pipeline.state import CONTRACTS, load_state, new_state, write_state_atomic
from agent_pipeline.tests import test_real_pipeline as base


class C07PostconditionTests(unittest.TestCase):
    setUp = base.RealPipelineTests.setUp
    tearDown = base.RealPipelineTests.tearDown
    config = base.RealPipelineTests.config
    write_fake_agent = base.RealPipelineTests.write_fake_agent
    verification_report = base.RealPipelineTests.verification_report
    stage_result = base.RealPipelineTests.stage_result

    def invoke_result(self, state, stage, output=None, failure=None, attempt=1, mutation=None):
        result = self.stage_result(stage, output or valid_artifact(stage), failure, attempt_number=attempt)
        result["_source_before"], source_error = controller.current_source_identity(self.task_dir)
        if source_error:
            result["_source_before_error"] = source_error
        excluded = controller.invocation_runtime_paths(
            self.task_dir, Path(result["candidate_artifact_path"]), controller.usage_ledger_path()
        )
        result["_protected_before"] = controller.capture_protected_integrity(self.task_dir, excluded)
        result["_protected_excluded_paths"] = [str(path) for path in excluded]
        if mutation is not None:
            mutation()
        return result

    def run_with_invocations(self, state, invocations, stage="02", mode="read-only", cfg=None):
        calls = []
        original = controller.invoke_stage

        def fake(*args, **kwargs):
            calls.append((args, kwargs))
            return invocations.pop(0)(state)

        controller.invoke_stage = fake
        try:
            code = controller.ensure_real_stage(self.task_dir, state, cfg or self.config(), stage, mode, {})
        finally:
            controller.invoke_stage = original
        return code, calls

    def test_fr1_normal_attempt_detects_change_to_already_dirty_file_and_stops_dispatch(self):
        dirty = self.root / "already_dirty.py"
        dirty.write_text("before\n", encoding="utf-8")
        state = new_state(self.task, "run-test")
        code, calls = self.run_with_invocations(state, [
            lambda current: self.invoke_result(current, "02", mutation=lambda: dirty.write_text("after\n", encoding="utf-8")),
            lambda current: self.invoke_result(current, "02"),
        ], cfg=dict(self.config(), stage_attempt_budget=2))
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(len(calls), 1)
        run = state["real_stage_runs"]["02"][-1]
        self.assertIn("untracked:already_dirty.py", run["postcondition"]["changed_paths"])
        self.assertFalse((self.task_dir / CONTRACTS["02"].filename).exists())

    def test_fr1_completion_retry_uses_same_guard_and_cannot_promote(self):
        dirty = self.root / "review_target.py"
        dirty.write_text("dirty-before\n", encoding="utf-8")
        state = new_state(self.task, "run-test")
        partial = "# Stage 2 - Technical specification\n\n## Summary\n\nUseful partial.\n"
        code, calls = self.run_with_invocations(state, [
            lambda current: self.invoke_result(current, "02", partial, FAILURE_CLASS_MAX_TURNS, attempt=1),
            lambda current: self.invoke_result(current, "02", valid_artifact("02"), attempt=2, mutation=lambda: dirty.write_text("dirty-after\n", encoding="utf-8")),
        ])
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(len(calls), 2)
        completion = state["real_stage_runs"]["02"][-1]
        self.assertEqual(completion["attempt_number"], 2)
        self.assertIn("untracked:review_target.py", completion["postcondition"]["changed_paths"])
        self.assertFalse((self.task_dir / CONTRACTS["02"].filename).exists())

    def test_fr1_overseer_mutation_blocks_without_fallback_or_automatic_acceptance(self):
        dirty = self.root / "overseer_target.py"
        dirty.write_text("dirty-before\n", encoding="utf-8")
        state = new_state(self.task, "run-test")
        original = controller.invoke_stage
        proposal = json.dumps({"route": "manual_test", "summary": [], "verified": [], "needs_human_testing": [], "known_limitations": [], "next_action": "test"})
        controller.invoke_stage = lambda *args, **kwargs: self.invoke_result(state, "overseer", proposal, mutation=lambda: dirty.write_text("dirty-after\n", encoding="utf-8"))
        try:
            handoff = controller.run_overseer_or_fallback(self.task_dir, state, self.config(), {"changed_files": []}, {}, None)
        finally:
            controller.invoke_stage = original
        self.assertIsNone(handoff)
        self.assertEqual(state["state"], "blocked")
        self.assertEqual(len(state["real_stage_runs"]["overseer"]), 1)
        self.assertFalse((self.task_dir / "05_supervisor_handoff.json").exists())
        self.assertFalse((self.task_dir / CONTRACTS["06"].filename).exists())

    def test_fr2_unavailable_source_comparison_blocks_promotion_and_retry(self):
        state = new_state(self.task, "run-test")
        def unavailable(current):
            result = self.invoke_result(current, "02")
            result["_source_before"] = None
            result["_source_before_error"] = "injected identity failure"
            return result
        code, calls = self.run_with_invocations(state, [unavailable, lambda current: self.invoke_result(current, "02")], cfg=dict(self.config(), stage_attempt_budget=2))
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(len(calls), 1)
        self.assertIn("comparison unavailable", state["last_failure"]["reason"])

    def test_fr3_unsupported_read_only_adapter_is_not_launched(self):
        state = new_state(self.task, "run-test")
        cfg = self.config()
        cfg["agents"]["codex"]["read_only"] = False
        calls = []
        original = controller.invoke_stage
        controller.invoke_stage = lambda *args, **kwargs: calls.append(args)
        try:
            code = controller.ensure_real_stage(self.task_dir, state, cfg, "02", "read-only", {})
        finally:
            controller.invoke_stage = original
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(calls, [])
        self.assertIn("no configured capable runner", state["last_failure"]["reason"])

    def test_fr4_implementation_cannot_tamper_with_controller_state(self):
        state = new_state(self.task, "run-test")
        write_state_atomic(self.task_dir, state)
        state_path = self.task_dir / ".orchestrator" / "state.json"
        code, calls = self.run_with_invocations(state, [
            lambda current: self.invoke_result(current, "05", mutation=lambda: state_path.write_text('{"forged": true}\n', encoding="utf-8")),
        ], stage="05", mode="workspace-write")
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(len(calls), 1)
        changed = state["real_stage_runs"]["05"][-1]["postcondition"]["protected_changed_paths"]
        self.assertIn(str(state_path), changed)
        self.assertFalse((self.task_dir / CONTRACTS["05"].filename).exists())

    def test_fr4_review_cannot_tamper_with_approved_input_or_policy(self):
        state = new_state(self.task, "run-test")
        config_path = self.root / ".agent-pipeline" / "config" / "orchestrator.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text('{"approved": true}\n', encoding="utf-8")
        approved = self.task_dir / CONTRACTS["00"].filename
        def tamper():
            approved.write_text("forged input\n", encoding="utf-8")
            config_path.write_text('{"approved": false}\n', encoding="utf-8")
        code, _calls = self.run_with_invocations(state, [lambda current: self.invoke_result(current, "07", mutation=tamper)], stage="07")
        self.assertEqual(code, EXIT_BLOCKED)
        changed = state["real_stage_runs"]["07"][-1]["postcondition"]["protected_changed_paths"]
        self.assertIn(str(approved), changed)
        self.assertIn(str(config_path), changed)
        self.assertFalse((self.task_dir / CONTRACTS["07"].filename).exists())

    def test_fr1_fr4_unchanged_attempts_reach_valid_automatic_acceptance(self):
        controller.verification.run_verification = lambda *args, **kwargs: self.verification_report(overall_status="passed", coverage_status="ok", driven_project_verified=True)
        code = controller.pipeline_run(self.task, allow_dirty=True)
        self.assertEqual(code, EXIT_SUCCESS)
        state = load_state(self.task_dir, self.task)
        self.assertEqual(state["state"], "complete")
        for stage in ("02", "03", "04", "04_gate", "07", "overseer"):
            agent_runs = [run for run in state["real_stage_runs"][stage] if run.get("provider") != "controller"]
            self.assertTrue(agent_runs)
            self.assertTrue(all(run["postcondition"]["valid"] for run in agent_runs))
        self.assertIn("- [x] Accept", (self.task_dir / CONTRACTS["08"].filename).read_text(encoding="utf-8"))
