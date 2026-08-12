from __future__ import print_function

import io
import inspect
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from agent_pipeline import controller
from agent_pipeline import gates
from agent_pipeline import prompts
from agent_pipeline import usage
from agent_pipeline.failures import EXIT_BAD_INPUT, EXIT_BLOCKED, EXIT_SUCCESS, EXIT_VALIDATION, FAILURE_CLASS_MALFORMED_ARTIFACT, FAILURE_CLASS_MAX_TURNS
from agent_pipeline.mock_agent import gate_artifact, valid_artifact
from agent_pipeline.runner import atomic_finalize
from agent_pipeline.state import CONTRACTS, load_state, new_state, reconcile_artifacts, state_path, write_state_atomic


class ControllerReliabilityTests(unittest.TestCase):
    def setUp(self):
        # Keep captured-stdout assertions independent from the caller's shell,
        # regardless of how the test runner imports this module.
        for key in ("FORCE_COLOR", "NO_COLOR"):
            original = os.environ.pop(key, None)
            if original is not None:
                self.addCleanup(os.environ.__setitem__, key, original)

    def with_tasks_root(self, root):
        original = controller.TASKS_ROOT
        controller.TASKS_ROOT = root
        self.addCleanup(lambda: setattr(controller, "TASKS_ROOT", original))

    def with_usage_root(self, root):
        original = controller.USAGE_ROOT
        controller.USAGE_ROOT = root
        self.addCleanup(lambda: setattr(controller, "USAGE_ROOT", original))

    def capture_dry_run(self, task):
        output = io.StringIO()
        with redirect_stdout(output):
            code = controller.dry_run(task)
        return code, output.getvalue()

    def capture_status(self, task):
        output = io.StringIO()
        with redirect_stdout(output):
            code = controller.status(task)
        return code, output.getvalue()

    def capture_verify(self, task):
        output = io.StringIO()
        with redirect_stdout(output):
            code = controller.pipeline_verify(task)
        return code, output.getvalue()

    def capture_usage(self, **kwargs):
        output = io.StringIO()
        with redirect_stdout(output):
            code = controller.pipeline_usage(**kwargs)
        return code, output.getvalue()

    def capture_run_background(self, task, allow_dirty=False):
        output = io.StringIO()
        with redirect_stdout(output):
            code = controller.pipeline_run_background(task, allow_dirty=allow_dirty)
        return code, output.getvalue()

    def capture_verify_background(self, task, run_build=False):
        output = io.StringIO()
        with redirect_stdout(output):
            code = controller.pipeline_verify_background(task, run_build=run_build)
        return code, output.getvalue()

    def test_task_dir_for_accepts_safe_task_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.with_tasks_root(root)

            for task in ("hardening-cheap-wins", "real-fixture", "task_1", "task.v2"):
                self.assertEqual(controller.task_dir_for(task), (root / task).resolve())

    def test_task_dir_for_rejects_unsafe_task_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.with_tasks_root(root)

            for task in (None, "", "/tmp/x", "../x", "x/../y", "x/y", "x\\y", ".hidden", "-dash"):
                with self.assertRaises(controller.ControllerError) as raised:
                    controller.task_dir_for(task)
                self.assertEqual(raised.exception.exit_code, EXIT_BAD_INPUT)

            self.assertEqual(list(root.iterdir()), [])

    def test_dry_run_reports_artifact_status_without_creating_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.with_tasks_root(root)

            code, output = self.capture_dry_run("dry-run-missing")

            self.assertEqual(code, EXIT_SUCCESS)
            self.assertIn("would_resume_at: 00", output)
            self.assertIn("artifact_status:", output)
            self.assertIn("00_original_request.md: stage=00 status=missing reason=missing", output)
            self.assertFalse(state_path(root / "dry-run-missing").exists())

    def test_dry_run_reports_stale_downstream_without_rewriting_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.with_tasks_root(root)
            task = "dry-run-stale"
            task_dir = root / task
            task_dir.mkdir(parents=True)
            for stage_key in ("00", "01", "02", "03"):
                (task_dir / CONTRACTS[stage_key].filename).write_text(valid_artifact(stage_key), encoding="utf-8")
            state = new_state(task)
            reconcile_artifacts(task_dir, state)
            write_state_atomic(task_dir, state)
            before = state_path(task_dir).read_text(encoding="utf-8")

            (task_dir / CONTRACTS["02"].filename).write_text(valid_artifact("02") + "\nChanged.\n", encoding="utf-8")
            code, output = self.capture_dry_run(task)
            after = state_path(task_dir).read_text(encoding="utf-8")

            self.assertEqual(code, EXIT_SUCCESS)
            self.assertEqual(before, after)
            self.assertIn("would_resume_at: 03", output)
            self.assertIn("03_audit.md: stage=03 status=valid reason=valid stale=true", output)
            self.assertIn("stale_downstream_stages: 03", output)

    def test_dry_run_reports_prefix_completion_with_downstream_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.with_tasks_root(root)
            task = "dry-run-prefix"
            task_dir = root / task
            task_dir.mkdir(parents=True)
            for stage_key in ("00", "01", "02", "04", "04_gate", "05"):
                (task_dir / CONTRACTS[stage_key].filename).write_text(valid_artifact(stage_key), encoding="utf-8")
            (task_dir / CONTRACTS["03"].filename).write_text(
                "# Stage 3 - Specification audit\n\nInvalid body.\n",
                encoding="utf-8",
            )
            state = new_state(task)
            write_state_atomic(task_dir, state)
            before = state_path(task_dir).read_text(encoding="utf-8")

            code, output = self.capture_dry_run(task)
            after = state_path(task_dir).read_text(encoding="utf-8")

            self.assertEqual(code, EXIT_SUCCESS)
            self.assertEqual(before, after)
            self.assertIn("would_resume_at: 03", output)
            self.assertIn("completed_stages: 00, 01, 02", output)
            self.assertIn("04_final_codex_brief.md: stage=04 status=valid reason=valid", output)
            self.assertIn("04_final_brief_audit.md: stage=04_gate status=valid reason=valid", output)

    def test_mock_test_passes_against_committed_fixtures(self):
        # Guards against agent_pipeline/fixtures/mock_scenarios.json drifting
        # out of sync with policies.py/controller.py again -- this fixture
        # drift previously went unnoticed by `unittest discover` entirely
        # because nothing here called controller.mock_test() directly (see
        # phase-3-handoff.md).
        output = io.StringIO()
        with redirect_stdout(output):
            code = controller.mock_test()
        self.assertEqual(code, EXIT_SUCCESS, output.getvalue())

    def test_checkpoint_noop_eligible_becomes_ineligible_once_stage6_is_valid(self):
        # Before Phase 3, a human hand-writing 06_manual_test_notes.md while
        # the controller sat at awaiting_human_test had no effect: the noop
        # check only ever looked at pre-Stage-6 artifact hashes. This is the
        # fix that lets a resumed pipeline-run actually drive Stage 7/8.
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "task"
            task_dir.mkdir(parents=True)
            state = {"state": "awaiting_human_test", "current_stage": "06", "human_checkpoint": {"noop_hashes": {}}}
            original = controller.stage5_postprocessing_complete
            controller.stage5_postprocessing_complete = lambda *args, **kwargs: {"valid": True}
            self.addCleanup(lambda: setattr(controller, "stage5_postprocessing_complete", original))

            result = controller.checkpoint_noop_eligible(task_dir, state)
            self.assertNotIn("Stage 6 manual test notes are ready", result["reason"])

            (task_dir / CONTRACTS["06"].filename).write_text(
                "# Stage 6 - Manual test notes\n\n## Decision\n\n- [x] Accept\n- [ ] Reject\n- [ ] Needs follow-up\n",
                encoding="utf-8",
            )
            result = controller.checkpoint_noop_eligible(task_dir, state)
            self.assertFalse(result["eligible"])
            self.assertIn("Stage 6 manual test notes are ready", result["reason"])

    def test_resume_reconciliation_skips_already_valid_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = "resume-reconcile"
            task_dir = Path(tmp) / task
            task_dir.mkdir(parents=True)
            for stage_key in ("00", "01", "02"):
                result = atomic_finalize(task_dir, stage_key, valid_artifact(stage_key))
                self.assertTrue(result["finalized"])
            state = new_state(task, "old-run")
            state["completed_stages"] = ["00", "01"]
            write_state_atomic(task_dir, state)
            loaded = load_state(task_dir, task)
            loaded["run_id"] = "run-test"

            code = controller.run_scenario(task_dir, task, loaded, {"actions": {}})

            self.assertEqual(code, EXIT_SUCCESS)
            self.assertIn("02", loaded["completed_stages"])
            self.assertEqual(loaded["agent_call_counts"].get("codex"), 3)

    def test_stage4_gate_loop_short_circuits_once_already_accepted(self):
        # Regression for the bug where every pipeline-run call re-entered
        # run_stage4_gate_loop even when Stage 4/04_gate were already
        # accepted, appending a fresh stage_gate_passes record each time and
        # eventually exhausting max_gate_passes purely from harmless
        # re-confirmations (see docs handoff / memory on this bug).
        with tempfile.TemporaryDirectory() as tmp:
            task = "stage4-gate-already-accepted"
            task_dir = Path(tmp) / task
            task_dir.mkdir(parents=True)
            for stage_key in ("00", "01", "02", "03", "04", "04_gate"):
                (task_dir / CONTRACTS[stage_key].filename).write_text(valid_artifact(stage_key), encoding="utf-8")
            state = new_state(task, "run-test")
            reconcile_artifacts(task_dir, state)
            self.assertIn("04_gate", state["completed_stages"])
            state["stage_gate_passes"] = [{"pass": 1, "accepted": True}]
            config = {"max_gate_passes": 2}

            for _ in range(3):
                code = controller.run_stage4_gate_loop(task_dir, state, config, {})
                self.assertEqual(code, EXIT_SUCCESS)

            self.assertEqual(len(state["stage_gate_passes"]), 1)

    def test_seed_validation_failure_clears_stale_completed_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = "seed-invalid"
            task_dir = Path(tmp) / task
            task_dir.mkdir(parents=True)
            state = new_state(task, "run-test")
            state["completed_stages"] = ["00", "01", "02"]
            state["current_stage"] = "03"

            code = controller.run_real_pipeline(task_dir, task, state, {}, allow_dirty=True)

            self.assertEqual(code, EXIT_BLOCKED)
            self.assertEqual(state["completed_stages"], [])
            self.assertEqual(state["current_stage"], "00")

    def test_cost_control_disabled_does_not_read_ledger_or_write_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = "cost-disabled"
            task_dir = Path(tmp) / task
            task_dir.mkdir(parents=True)
            for stage_key in ("00", "01"):
                (task_dir / CONTRACTS[stage_key].filename).write_text(valid_artifact(stage_key), encoding="utf-8")
            state = new_state(task, "run-test")
            events = []

            original_read_entries = controller.usage.read_entries
            original_append_log = controller.append_log
            original_ensure = controller.ensure_real_stage
            controller.usage.read_entries = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("ledger read"))
            controller.append_log = lambda task_dir_arg, event: events.append(event)
            controller.ensure_real_stage = lambda *args, **kwargs: EXIT_BLOCKED
            self.addCleanup(lambda: setattr(controller.usage, "read_entries", original_read_entries))
            self.addCleanup(lambda: setattr(controller, "append_log", original_append_log))
            self.addCleanup(lambda: setattr(controller, "ensure_real_stage", original_ensure))

            code = controller.run_real_pipeline(task_dir, task, state, {}, allow_dirty=True)

            self.assertEqual(code, EXIT_BLOCKED)
            self.assertNotIn("stage_overrides", state)
            self.assertNotIn("cost_policy_applied", [event.get("event") for event in events])

    def test_cost_control_enabled_with_disabled_ledger_records_empty_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = "cost-enabled-ledger-disabled"
            task_dir = Path(tmp) / task
            task_dir.mkdir(parents=True)
            for stage_key in ("00", "01"):
                (task_dir / CONTRACTS[stage_key].filename).write_text(valid_artifact(stage_key), encoding="utf-8")
            state = new_state(task, "run-test")
            events = []
            cfg = {
                "cost_control": {
                    "enabled": True,
                    "min_samples": 2,
                    "max_retry_rate": 0.5,
                    "eligible_stages": ["02"],
                    "downgrade_candidates": {"claude": {"effort": "low"}},
                },
                "usage_ledger": {"enabled": False},
                "roles": {"02": {"primary": "claude", "fallbacks": []}},
                "max_gate_passes": 1,
            }

            original_read_entries = controller.usage.read_entries
            original_append_log = controller.append_log
            original_ensure = controller.ensure_real_stage
            controller.usage.read_entries = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("ledger read"))
            controller.append_log = lambda task_dir_arg, event: events.append(event)
            controller.ensure_real_stage = lambda *args, **kwargs: EXIT_BLOCKED
            self.addCleanup(lambda: setattr(controller.usage, "read_entries", original_read_entries))
            self.addCleanup(lambda: setattr(controller, "append_log", original_append_log))
            self.addCleanup(lambda: setattr(controller, "ensure_real_stage", original_ensure))

            code = controller.run_real_pipeline(task_dir, task, state, cfg, allow_dirty=True)

            self.assertEqual(code, EXIT_BLOCKED)
            self.assertEqual(state["stage_overrides"], {})
            self.assertEqual([event for event in events if event.get("event") == "cost_policy_applied"][0]["overrides"], {})

    def test_cost_control_quality_false_does_not_read_outcome_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = "cost-quality-disabled"
            task_dir = Path(tmp) / task
            task_dir.mkdir(parents=True)
            for stage_key in ("00", "01"):
                (task_dir / CONTRACTS[stage_key].filename).write_text(valid_artifact(stage_key), encoding="utf-8")
            state = new_state(task, "run-test")
            cfg = {
                "cost_control": {
                    "enabled": True,
                    "quality_aware": False,
                    "min_samples": 2,
                    "max_retry_rate": 0.5,
                    "eligible_stages": ["02"],
                    "downgrade_candidates": {"claude": {"effort": "low"}},
                },
                "usage_ledger": {"enabled": True},
                "roles": {"02": {"primary": "claude", "fallbacks": []}},
                "max_gate_passes": 1,
            }
            seen_paths = []

            original_read_entries = controller.usage.read_entries
            original_ensure = controller.ensure_real_stage
            controller.usage.read_entries = lambda path: seen_paths.append(path) or []
            controller.ensure_real_stage = lambda *args, **kwargs: EXIT_BLOCKED
            self.addCleanup(lambda: setattr(controller.usage, "read_entries", original_read_entries))
            self.addCleanup(lambda: setattr(controller, "ensure_real_stage", original_ensure))

            code = controller.run_real_pipeline(task_dir, task, state, cfg, allow_dirty=True)

            self.assertEqual(code, EXIT_BLOCKED)
            self.assertEqual(seen_paths, [controller.usage_ledger_path()])

    def test_cost_control_quality_true_without_stage4_eligible_skips_outcome_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = "cost-quality-enabled"
            task_dir = Path(tmp) / task
            task_dir.mkdir(parents=True)
            for stage_key in ("00", "01"):
                (task_dir / CONTRACTS[stage_key].filename).write_text(valid_artifact(stage_key), encoding="utf-8")
            state = new_state(task, "run-test")
            cfg = {
                "cost_control": {
                    "enabled": True,
                    "quality_aware": True,
                    "min_samples": 2,
                    "max_retry_rate": 0.5,
                    "max_rejection_rate": 0.5,
                    "eligible_stages": ["02"],
                    "downgrade_candidates": {"claude": {"effort": "low"}},
                },
                "usage_ledger": {"enabled": True},
                "roles": {"02": {"primary": "claude", "fallbacks": []}},
                "max_gate_passes": 1,
            }
            seen_paths = []

            original_read_entries = controller.usage.read_entries
            original_ensure = controller.ensure_real_stage
            controller.usage.read_entries = lambda path: seen_paths.append(path) or []
            controller.ensure_real_stage = lambda *args, **kwargs: EXIT_BLOCKED
            self.addCleanup(lambda: setattr(controller.usage, "read_entries", original_read_entries))
            self.addCleanup(lambda: setattr(controller, "ensure_real_stage", original_ensure))

            code = controller.run_real_pipeline(task_dir, task, state, cfg, allow_dirty=True)

            self.assertEqual(code, EXIT_BLOCKED)
            self.assertEqual(seen_paths, [controller.usage_ledger_path()])

    def test_cost_control_quality_true_with_stage4_eligible_reads_outcome_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = "cost-quality-stage4-eligible"
            task_dir = Path(tmp) / task
            task_dir.mkdir(parents=True)
            for stage_key in ("00", "01"):
                (task_dir / CONTRACTS[stage_key].filename).write_text(valid_artifact(stage_key), encoding="utf-8")
            state = new_state(task, "run-test")
            cfg = {
                "cost_control": {
                    "enabled": True,
                    "quality_aware": True,
                    "min_samples": 2,
                    "max_retry_rate": 0.5,
                    "max_rejection_rate": 0.5,
                    "eligible_stages": ["04"],
                    "downgrade_candidates": {"claude": {"effort": "low"}},
                },
                "usage_ledger": {"enabled": True},
                "roles": {"04": {"primary": "claude", "fallbacks": []}},
                "max_gate_passes": 1,
            }
            seen_paths = []

            original_read_entries = controller.usage.read_entries
            original_ensure = controller.ensure_real_stage
            controller.usage.read_entries = lambda path: seen_paths.append(path) or []
            controller.ensure_real_stage = lambda *args, **kwargs: EXIT_BLOCKED
            self.addCleanup(lambda: setattr(controller.usage, "read_entries", original_read_entries))
            self.addCleanup(lambda: setattr(controller, "ensure_real_stage", original_ensure))

            code = controller.run_real_pipeline(task_dir, task, state, cfg, allow_dirty=True)

            self.assertEqual(code, EXIT_BLOCKED)
            self.assertEqual(seen_paths, [controller.usage_ledger_path(), controller.outcomes_ledger_path()])

    def test_merge_stage_override_returns_fresh_config_without_mutating_original(self):
        original = {
            "roles": {
                "02": {"primary": "claude", "fallbacks": []},
                "03": {"primary": "codex", "fallbacks": []},
            },
            "other": True,
        }

        merged = controller.merge_stage_override_into_config(original, "02", {"model": "cheap", "effort": "low"})

        self.assertIsNot(merged, original)
        self.assertIsNot(merged["roles"], original["roles"])
        self.assertIsNot(merged["roles"]["02"], original["roles"]["02"])
        self.assertIs(merged["roles"]["03"], original["roles"]["03"])
        self.assertEqual(merged["roles"]["02"]["model_override"], "cheap")
        self.assertEqual(merged["roles"]["02"]["effort_override"], "low")
        self.assertNotIn("model_override", original["roles"]["02"])

    def test_ensure_real_stage_applies_override_only_for_selected_agent_and_reuses_for_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = "cost-dispatch"
            task_dir = Path(tmp) / task
            task_dir.mkdir(parents=True)
            state = new_state(task, "run-test")
            state["stage_overrides"] = {"02": {"claude": {"model": "cheap-claude", "effort": "low"}}}
            cfg = {
                "stage_attempt_budget": 1,
                "roles": {"02": {"primary": "claude", "fallbacks": ["codex"]}},
                "agents": {
                    "claude": {"enabled": True, "workspace_write": False},
                    "codex": {"enabled": True, "workspace_write": False},
                },
                "cross_task_cooldowns": {"enabled": False},
                "usage_ledger": {"enabled": False},
                "reasoning_capture": {"enabled": True},
                "cost_control": {"enabled": True},
            }
            calls = []

            def fake_invoke(task_dir_arg, state_arg, config_arg, stage_key, execution_mode, agent, pass_number, **kwargs):
                calls.append(config_arg)
                candidate_path = task_dir_arg / ("candidate-%d.md" % len(calls))
                if len(calls) == 1:
                    candidate_path.write_text("# Stage 2 - Technical specification\n\n## Summary\n\nPartial.\n", encoding="utf-8")
                    failure_class = FAILURE_CLASS_MAX_TURNS
                else:
                    candidate_path.write_text(valid_artifact("02"), encoding="utf-8")
                    failure_class = None
                return {
                    "candidate_artifact_path": str(candidate_path),
                    "failure_class": failure_class,
                    "exit_code": 1 if failure_class else 0,
                    "metadata_path": str(candidate_path) + ".json",
                    "attempt_number": kwargs.get("attempt_number"),
                    "_source_before": "",
                }

            original_invoke = controller.invoke_stage
            original_source_snapshot = controller.source_snapshot
            controller.invoke_stage = fake_invoke
            controller.source_snapshot = lambda: ""
            self.addCleanup(lambda: setattr(controller, "invoke_stage", original_invoke))
            self.addCleanup(lambda: setattr(controller, "source_snapshot", original_source_snapshot))

            code = controller.ensure_real_stage(task_dir, state, cfg, "02", "read-only", {})

            self.assertEqual(code, EXIT_SUCCESS)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0]["roles"]["02"]["model_override"], "cheap-claude")
            self.assertEqual(calls[0]["roles"]["02"]["effort_override"], "low")
            self.assertEqual(calls[1]["roles"]["02"]["model_override"], "cheap-claude")
            self.assertNotIn("model_override", cfg["roles"]["02"])

    def test_ensure_real_stage_does_not_apply_override_for_different_selected_agent(self):
        state = new_state("task", "run-test")
        state["stage_overrides"] = {"02": {"claude": {"model": "cheap-claude"}}}
        cfg = {
            "stage_attempt_budget": 1,
            "roles": {"02": {"primary": "codex", "fallbacks": []}},
            "agents": {"codex": {"enabled": True, "workspace_write": False}},
            "cross_task_cooldowns": {"enabled": False},
            "cost_control": {"enabled": True},
        }

        selected = controller.merge_matching_stage_override_into_config(cfg, state, "02", "codex")

        self.assertIs(selected, cfg)

    def test_ensure_real_stage_honors_persisted_attempt_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = "attempt-budget"
            task_dir = Path(tmp) / task
            task_dir.mkdir(parents=True)
            state = new_state(task, "run-test")
            state["attempts"]["02"] = 2
            cfg = {
                "stage_attempt_budget": 2,
                "roles": {"02": {"primary": "codex", "fallbacks": []}},
                "agents": {"codex": {"enabled": True, "workspace_write": False}},
                "cross_task_cooldowns": {"enabled": False},
                "cost_control": {"enabled": False},
            }
            calls = []
            original_invoke = controller.invoke_stage
            controller.invoke_stage = lambda *args, **kwargs: calls.append(args) or {}
            self.addCleanup(lambda: setattr(controller, "invoke_stage", original_invoke))

            code = controller.ensure_real_stage(task_dir, state, cfg, "02", "read-only", {})

            self.assertEqual(code, EXIT_BLOCKED)
            self.assertEqual(calls, [])
            self.assertEqual(state["last_failure"]["reason"], "attempt budget exhausted")

    def test_forced_stage_pass_uses_fresh_retry_budget_despite_cumulative_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = "forced-pass-budget"
            task_dir = Path(tmp) / task
            task_dir.mkdir(parents=True)
            state = new_state(task, "run-test")
            state["attempts"]["04"] = 2
            cfg = {
                "stage_attempt_budget": 2,
                "roles": {"04": {"primary": "codex", "fallbacks": []}},
                "agents": {"codex": {"enabled": True, "workspace_write": False}},
                "cross_task_cooldowns": {"enabled": False},
                "cost_control": {"enabled": False},
            }
            calls = []

            def fake_invoke(task_dir_arg, state_arg, config_arg, stage_key, execution_mode, agent, pass_number, **kwargs):
                calls.append(kwargs.get("attempt_number"))
                candidate_path = task_dir_arg / ("attempt-%s.md" % kwargs.get("attempt_number"))
                candidate_path.write_text("# malformed\n", encoding="utf-8")
                return {
                    "candidate_artifact_path": str(candidate_path),
                    "failure_class": FAILURE_CLASS_MALFORMED_ARTIFACT,
                    "exit_code": 1,
                    "metadata_path": str(candidate_path) + ".json",
                    "_source_before": "",
                }

            original_invoke = controller.invoke_stage
            original_source_snapshot = controller.source_snapshot
            controller.invoke_stage = fake_invoke
            controller.source_snapshot = lambda: ""
            self.addCleanup(lambda: setattr(controller, "invoke_stage", original_invoke))
            self.addCleanup(lambda: setattr(controller, "source_snapshot", original_source_snapshot))

            code = controller.ensure_real_stage(task_dir, state, cfg, "04", "read-only", {}, pass_number=2, force=True)

            self.assertEqual(code, EXIT_BLOCKED)
            self.assertEqual(calls, [3, 4])
            self.assertEqual(state["last_failure"]["stage"], "04")

    def test_merge_matching_stage_override_ignored_when_cost_control_disabled(self):
        state = new_state("task", "run-test")
        state["stage_overrides"] = {"02": {"codex": {"model": "cheap-codex"}}}
        cfg = {
            "stage_attempt_budget": 1,
            "roles": {"02": {"primary": "codex", "fallbacks": []}},
            "agents": {"codex": {"enabled": True, "workspace_write": False}},
            "cross_task_cooldowns": {"enabled": False},
            "cost_control": {"enabled": False},
        }

        selected = controller.merge_matching_stage_override_into_config(cfg, state, "02", "codex")

        self.assertIs(selected, cfg)

    def test_fresh_stage5_success_is_checkpointed_before_postprocessing(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = "stage5-checkpoint"
            task_dir = Path(tmp) / task
            task_dir.mkdir(parents=True)
            for stage_key in ("00", "01", "02", "03", "04", "04_gate"):
                (task_dir / CONTRACTS[stage_key].filename).write_text(valid_artifact(stage_key), encoding="utf-8")
            state = new_state(task, "run-test")

            def fake_ensure(task_dir_arg, state_arg, config_arg, stage_key, execution_mode, assignments, pass_number=1, force=False, extra_context=None):
                if stage_key == "05":
                    (task_dir_arg / CONTRACTS["05"].filename).write_text(valid_artifact("05"), encoding="utf-8")
                return EXIT_SUCCESS

            def fail_postprocessing(*args, **kwargs):
                raise RuntimeError("postprocessing reached")

            original_ensure = controller.ensure_real_stage
            original_report = controller.stage5_report_provenance
            original_capture = controller.capture_dirty_baseline
            controller.ensure_real_stage = fake_ensure
            controller.stage5_report_provenance = fail_postprocessing
            controller.capture_dirty_baseline = lambda root: {"captured_at": "now", "entries": [], "hashes": {}}
            self.addCleanup(lambda: setattr(controller, "ensure_real_stage", original_ensure))
            self.addCleanup(lambda: setattr(controller, "stage5_report_provenance", original_report))
            self.addCleanup(lambda: setattr(controller, "capture_dirty_baseline", original_capture))

            with self.assertRaisesRegex(RuntimeError, "postprocessing reached"):
                controller.run_real_pipeline(task_dir, task, state, {"max_gate_passes": 1}, allow_dirty=True)

            persisted = load_state(task_dir, task)
            self.assertIn("05", persisted["completed_stages"])
            self.assertEqual(persisted["current_stage"], "06")

    def test_stage4_gate_loop_does_not_short_circuit_rejected_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = "stage4-gate-rejected"
            task_dir = Path(tmp) / task
            task_dir.mkdir(parents=True)
            for stage_key in ("00", "01", "02", "03", "04"):
                (task_dir / CONTRACTS[stage_key].filename).write_text(valid_artifact(stage_key), encoding="utf-8")
            (task_dir / CONTRACTS["04_gate"].filename).write_text(
                gate_artifact(
                    "04_gate",
                    "ready_for_implementation: false\nblocking_issues: []\nnonblocking_issues: []\nrequired_revision_targets: []",
                ),
                encoding="utf-8",
            )
            state = new_state(task, "run-test")
            reconcile_artifacts(task_dir, state)
            self.assertIn("04_gate", state["completed_stages"])
            config = {"max_gate_passes": 1}

            code = controller.run_stage4_gate_loop(task_dir, state, config, {})

            self.assertEqual(code, EXIT_BLOCKED)
            self.assertEqual(state["last_failure"]["stage"], "04_gate")
            self.assertEqual(state["last_failure"]["failure_class"], "gate_pass_limit_exhausted")
            self.assertEqual(state["completed_stages"], ["00", "01", "02", "03", "04"])
            self.assertEqual(len(state["stage_gate_passes"]), 1)
            self.assertFalse(state["stage_gate_passes"][0]["accepted"])

    def test_stage4_gate_loop_forces_pass2_after_rejected_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = "stage4-gate-resume-pass2"
            task_dir = Path(tmp) / task
            task_dir.mkdir(parents=True)
            for stage_key in ("00", "01", "02", "03", "04"):
                (task_dir / CONTRACTS[stage_key].filename).write_text(valid_artifact(stage_key), encoding="utf-8")
            (task_dir / CONTRACTS["04_gate"].filename).write_text(
                gate_artifact(
                    "04_gate",
                    "ready_for_implementation: false\nblocking_issues: []\nnonblocking_issues: []\nrequired_revision_targets: []",
                ),
                encoding="utf-8",
            )
            state = new_state(task, "run-test")
            reconcile_artifacts(task_dir, state)
            self.assertIn("04", state["completed_stages"])
            self.assertIn("04_gate", state["completed_stages"])
            state["stage_gate_passes"] = [{"pass": 1, "accepted": False}]
            calls = []

            def fake_ensure(task_dir_arg, state_arg, config_arg, stage_key, execution_mode, assignments, pass_number=1, force=False, extra_context=None):
                calls.append((stage_key, pass_number, force, extra_context))
                (task_dir_arg / CONTRACTS[stage_key].filename).write_text(valid_artifact(stage_key), encoding="utf-8")
                if stage_key not in state_arg["completed_stages"]:
                    state_arg["completed_stages"].append(stage_key)
                return EXIT_SUCCESS

            original = controller.ensure_real_stage
            controller.ensure_real_stage = fake_ensure
            self.addCleanup(lambda: setattr(controller, "ensure_real_stage", original))

            code = controller.run_stage4_gate_loop(task_dir, state, {"max_gate_passes": 2}, {})

            self.assertEqual(code, EXIT_SUCCESS)
            self.assertEqual(calls[0][:3], ("04", 2, True))
            self.assertIn("Latest Stage 04 gate rejection feedback:", calls[0][3])
            self.assertIn("ready_for_implementation: false", calls[0][3])
            self.assertEqual(calls[1], ("04_gate", 2, True, None))

    def test_stage4_gate_loop_uses_persisted_rejection_hash_on_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = "stage4-identical-resume"
            task_dir = Path(tmp) / task
            task_dir.mkdir(parents=True)
            for stage_key in ("00", "01", "02", "03"):
                (task_dir / CONTRACTS[stage_key].filename).write_text(valid_artifact(stage_key), encoding="utf-8")
            brief = valid_artifact("04")
            audit = gate_artifact(
                "04_gate",
                "ready_for_implementation: false\nblocking_issues: []\nnonblocking_issues: []\nrequired_revision_targets: []",
            )
            (task_dir / CONTRACTS["04"].filename).write_text(brief, encoding="utf-8")
            (task_dir / CONTRACTS["04_gate"].filename).write_text(audit, encoding="utf-8")
            state = new_state(task, "run-test")
            state["stage_gate_passes"] = [{"pass": 1, "accepted": False}]
            state["stage4_previous_rejection"] = {
                "brief_hash": controller.sha256_file(task_dir / CONTRACTS["04"].filename),
                "audit_hash": controller.sha256_file(task_dir / CONTRACTS["04_gate"].filename),
            }
            calls = []

            def fake_ensure(task_dir_arg, state_arg, config_arg, stage_key, execution_mode, assignments, pass_number=1, force=False, extra_context=None):
                calls.append(stage_key)
                if stage_key == "04":
                    (task_dir_arg / CONTRACTS["04"].filename).write_text(brief, encoding="utf-8")
                    return EXIT_SUCCESS
                if stage_key == "04_gate":
                    self.fail("04_gate should not run for an identical resumed rejection")
                return EXIT_SUCCESS

            original = controller.ensure_real_stage
            controller.ensure_real_stage = fake_ensure
            self.addCleanup(lambda: setattr(controller, "ensure_real_stage", original))

            code = controller.run_stage4_gate_loop(task_dir, state, {"max_gate_passes": 2}, {})

            self.assertEqual(code, EXIT_BLOCKED)
            self.assertEqual(calls, ["04"])
            self.assertEqual(state["last_failure"]["failure_class"], "gate_rejected")

    def test_stage4_gate_loop_pass1_force_still_tracks_completed_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = "stage4-gate-pass1-force"
            task_dir = Path(tmp) / task
            task_dir.mkdir(parents=True)
            for stage_key in ("00", "01", "02", "03", "04"):
                (task_dir / CONTRACTS[stage_key].filename).write_text(valid_artifact(stage_key), encoding="utf-8")
            (task_dir / CONTRACTS["04_gate"].filename).write_text(
                gate_artifact(
                    "04_gate",
                    "ready_for_implementation: false\nblocking_issues: []\nnonblocking_issues: []\nrequired_revision_targets: []",
                ),
                encoding="utf-8",
            )
            state = new_state(task, "run-test")
            reconcile_artifacts(task_dir, state)
            calls = []

            def fake_ensure(task_dir_arg, state_arg, config_arg, stage_key, execution_mode, assignments, pass_number=1, force=False, extra_context=None):
                calls.append((stage_key, pass_number, force, extra_context))
                return EXIT_SUCCESS

            original = controller.ensure_real_stage
            controller.ensure_real_stage = fake_ensure
            self.addCleanup(lambda: setattr(controller, "ensure_real_stage", original))

            code = controller.run_stage4_gate_loop(task_dir, state, {"max_gate_passes": 1}, {})

            self.assertEqual(code, EXIT_BLOCKED)
            self.assertEqual(calls, [("04", 1, False, None), ("04_gate", 1, False, None)])

    def test_stage4_gate_loop_records_outcome_for_finalized_stage4_producer(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = "stage4-quality-outcome"
            root = Path(tmp)
            task_dir = root / task
            task_dir.mkdir(parents=True)
            self.with_usage_root(root / "usage")
            state = new_state(task, "run-test")

            def fake_ensure(task_dir_arg, state_arg, config_arg, stage_key, execution_mode, assignments, pass_number=1, force=False, extra_context=None):
                if stage_key == "04":
                    (task_dir_arg / CONTRACTS["04"].filename).write_text(valid_artifact("04"), encoding="utf-8")
                    state_arg.setdefault("real_stage_runs", {}).setdefault("04", []).append({
                        "agent": "codex",
                        "model": "wrong-first-model",
                        "pass_number": pass_number,
                        "finalized": False,
                    })
                    state_arg.setdefault("real_stage_runs", {}).setdefault("04", []).append({
                        "agent": "claude",
                        "model": "claude-haiku-4-5",
                        "pass_number": pass_number,
                        "finalized": True,
                        "final_artifact_hash": controller.sha256_file(task_dir_arg / CONTRACTS["04"].filename),
                    })
                if stage_key == "04_gate":
                    (task_dir_arg / CONTRACTS["04_gate"].filename).write_text(
                        gate_artifact(
                            "04_gate",
                            "ready_for_implementation: true\nblocking_issues: []\nnonblocking_issues: []\nrequired_revision_targets: []",
                        ),
                        encoding="utf-8",
                    )
                return EXIT_SUCCESS

            original = controller.ensure_real_stage
            controller.ensure_real_stage = fake_ensure
            self.addCleanup(lambda: setattr(controller, "ensure_real_stage", original))

            code = controller.run_stage4_gate_loop(
                task_dir,
                state,
                {"max_gate_passes": 1, "usage_ledger": {"enabled": True}, "cost_control": {"quality_aware": True}},
                {},
            )

            self.assertEqual(code, EXIT_SUCCESS)
            entries = usage.read_entries(controller.outcomes_ledger_path())
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["task"], task)
            self.assertEqual(entries[0]["run_id"], "run-test")
            self.assertEqual(entries[0]["stage"], "04")
            self.assertEqual(entries[0]["agent"], "claude")
            self.assertEqual(entries[0]["model"], "claude-haiku-4-5")
            self.assertEqual(entries[0]["pass_number"], 1)
            self.assertTrue(entries[0]["accepted"])
            self.assertEqual(entries[0]["classification"], "accepted")

    def test_stage4_quality_outcome_logs_when_append_returns_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = "stage4-quality-append-false"
            task_dir = Path(tmp) / task
            task_dir.mkdir(parents=True)
            (task_dir / CONTRACTS["04"].filename).write_text(valid_artifact("04"), encoding="utf-8")
            state = new_state(task, "run-test")
            state.setdefault("real_stage_runs", {}).setdefault("04", []).append({
                "agent": "claude",
                "model": "claude-haiku-4-5",
                "pass_number": 1,
                "finalized": True,
                "final_artifact_hash": controller.sha256_file(task_dir / CONTRACTS["04"].filename),
            })
            events = []
            gate = {"accepted": True, "valid": True}

            original_append_entry = gates.usage.append_entry
            original_append_log = gates.append_log
            gates.usage.append_entry = lambda *args, **kwargs: False
            gates.append_log = lambda task_dir_arg, event: events.append(event)
            self.addCleanup(lambda: setattr(gates.usage, "append_entry", original_append_entry))
            self.addCleanup(lambda: setattr(gates, "append_log", original_append_log))

            result = gates.record_stage4_quality_outcome(
                task_dir,
                state,
                {"cost_control": {"quality_aware": True}},
                1,
                gate,
                task_dir / "outcomes.jsonl",
            )

            self.assertFalse(result)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event"], "stage4_quality_outcome_write_failed")
            self.assertEqual(events[0]["stage"], "04_gate")
            self.assertEqual(events[0]["pass"], 1)
            self.assertEqual(events[0]["run_id"], "run-test")
            self.assertEqual(events[0]["reason"], "append_entry_returned_false")

    def test_stage4_quality_outcome_logs_exception_without_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = "stage4-quality-exception"
            task_dir = Path(tmp) / task
            task_dir.mkdir(parents=True)
            state = new_state(task, "run-test")
            events = []
            gate = {"accepted": True, "valid": True}

            original_producer = gates.finalized_stage4_producer
            original_append_log = gates.append_log
            gates.finalized_stage4_producer = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("forced failure"))
            gates.append_log = lambda task_dir_arg, event: events.append(event)
            self.addCleanup(lambda: setattr(gates, "finalized_stage4_producer", original_producer))
            self.addCleanup(lambda: setattr(gates, "append_log", original_append_log))

            result = gates.record_stage4_quality_outcome(
                task_dir,
                state,
                {"cost_control": {"quality_aware": True}},
                2,
                gate,
                task_dir / "outcomes.jsonl",
            )

            self.assertFalse(result)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event"], "stage4_quality_outcome_write_failed")
            self.assertEqual(events[0]["stage"], "04_gate")
            self.assertEqual(events[0]["pass"], 2)
            self.assertEqual(events[0]["run_id"], "run-test")
            self.assertEqual(events[0]["error"], "forced failure")

    def test_stage4_quality_outcome_swallows_failure_log_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = "stage4-quality-log-failure"
            task_dir = Path(tmp) / task
            task_dir.mkdir(parents=True)
            (task_dir / CONTRACTS["04"].filename).write_text(valid_artifact("04"), encoding="utf-8")
            state = new_state(task, "run-test")
            state.setdefault("real_stage_runs", {}).setdefault("04", []).append({
                "agent": "claude",
                "model": "claude-haiku-4-5",
                "pass_number": 1,
                "finalized": True,
                "final_artifact_hash": controller.sha256_file(task_dir / CONTRACTS["04"].filename),
            })

            original_append_entry = gates.usage.append_entry
            original_append_log = gates.append_log
            gates.usage.append_entry = lambda *args, **kwargs: False
            gates.append_log = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("log failure"))
            self.addCleanup(lambda: setattr(gates.usage, "append_entry", original_append_entry))
            self.addCleanup(lambda: setattr(gates, "append_log", original_append_log))

            result = gates.record_stage4_quality_outcome(
                task_dir,
                state,
                {"cost_control": {"quality_aware": True}},
                1,
                {"accepted": True, "valid": True},
                task_dir / "outcomes.jsonl",
            )

            self.assertFalse(result)

    def test_stage4_gate_loop_skips_outcome_when_quality_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = "stage4-quality-disabled"
            root = Path(tmp)
            task_dir = root / task
            task_dir.mkdir(parents=True)
            self.with_usage_root(root / "usage")
            state = new_state(task, "run-test")

            def fake_ensure(task_dir_arg, state_arg, config_arg, stage_key, execution_mode, assignments, pass_number=1, force=False, extra_context=None):
                (task_dir_arg / CONTRACTS[stage_key].filename).write_text(
                    gate_artifact(
                        stage_key,
                        "ready_for_implementation: true\nblocking_issues: []\nnonblocking_issues: []\nrequired_revision_targets: []",
                    ) if stage_key == "04_gate" else valid_artifact(stage_key),
                    encoding="utf-8",
                )
                return EXIT_SUCCESS

            original = controller.ensure_real_stage
            controller.ensure_real_stage = fake_ensure
            self.addCleanup(lambda: setattr(controller, "ensure_real_stage", original))

            code = controller.run_stage4_gate_loop(
                task_dir,
                state,
                {"max_gate_passes": 1, "usage_ledger": {"enabled": True}, "cost_control": {"quality_aware": False}},
                {},
            )

            self.assertEqual(code, EXIT_SUCCESS)
            self.assertFalse(controller.outcomes_ledger_path().exists())

    def test_stage4_gate_loop_skips_outcome_when_gate_artifact_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = "stage4-quality-invalid-gate"
            root = Path(tmp)
            task_dir = root / task
            task_dir.mkdir(parents=True)
            self.with_usage_root(root / "usage")
            state = new_state(task, "run-test")

            def fake_ensure(task_dir_arg, state_arg, config_arg, stage_key, execution_mode, assignments, pass_number=1, force=False, extra_context=None):
                if stage_key == "04":
                    (task_dir_arg / CONTRACTS["04"].filename).write_text(valid_artifact("04"), encoding="utf-8")
                    state_arg.setdefault("real_stage_runs", {}).setdefault("04", []).append({
                        "agent": "claude",
                        "model": "claude-haiku-4-5",
                        "pass_number": pass_number,
                        "finalized": True,
                        "final_artifact_hash": controller.sha256_file(task_dir_arg / CONTRACTS["04"].filename),
                    })
                if stage_key == "04_gate":
                    # Unquoted "#" inside the YAML gate block trips parse_gate's
                    # malformed-syntax check, producing a reviewer-side defect
                    # (valid: False) rather than a real verdict on the brief.
                    (task_dir_arg / CONTRACTS["04_gate"].filename).write_text(
                        gate_artifact("04_gate", "ready_for_implementation: true # stray comment"),
                        encoding="utf-8",
                    )
                return EXIT_SUCCESS

            original = controller.ensure_real_stage
            controller.ensure_real_stage = fake_ensure
            self.addCleanup(lambda: setattr(controller, "ensure_real_stage", original))

            code = controller.run_stage4_gate_loop(
                task_dir,
                state,
                {"max_gate_passes": 1, "usage_ledger": {"enabled": True}, "cost_control": {"quality_aware": True}},
                {},
            )

            self.assertEqual(code, EXIT_BLOCKED)
            self.assertEqual(usage.read_entries(controller.outcomes_ledger_path()), [])

    def test_stage4_gate_loop_skips_outcome_when_producer_unattributed(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = "stage4-quality-no-producer"
            root = Path(tmp)
            task_dir = root / task
            task_dir.mkdir(parents=True)
            self.with_usage_root(root / "usage")
            state = new_state(task, "run-test")

            def fake_ensure(task_dir_arg, state_arg, config_arg, stage_key, execution_mode, assignments, pass_number=1, force=False, extra_context=None):
                if stage_key == "04":
                    (task_dir_arg / CONTRACTS["04"].filename).write_text(valid_artifact("04"), encoding="utf-8")
                    # No real_stage_runs["04"] entries recorded at all, so
                    # finalized_stage4_producer has nothing to attribute to.
                if stage_key == "04_gate":
                    (task_dir_arg / CONTRACTS["04_gate"].filename).write_text(
                        gate_artifact(
                            "04_gate",
                            "ready_for_implementation: true\nblocking_issues: []\nnonblocking_issues: []\nrequired_revision_targets: []",
                        ),
                        encoding="utf-8",
                    )
                return EXIT_SUCCESS

            original = controller.ensure_real_stage
            controller.ensure_real_stage = fake_ensure
            self.addCleanup(lambda: setattr(controller, "ensure_real_stage", original))

            code = controller.run_stage4_gate_loop(
                task_dir,
                state,
                {"max_gate_passes": 1, "usage_ledger": {"enabled": True}, "cost_control": {"quality_aware": True}},
                {},
            )

            self.assertEqual(code, EXIT_SUCCESS)
            self.assertEqual(usage.read_entries(controller.outcomes_ledger_path()), [])

    def test_stage4_gate_loop_archives_brief_before_identical_revision_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = "stage4-archive-identical"
            task_dir = Path(tmp) / task
            task_dir.mkdir(parents=True)
            for stage_key in ("00", "01", "02", "03"):
                (task_dir / CONTRACTS[stage_key].filename).write_text(valid_artifact(stage_key), encoding="utf-8")
            state = new_state(task, "run-test")
            calls = {"04_gate": 0}

            def fake_ensure(task_dir_arg, state_arg, config_arg, stage_key, execution_mode, assignments, pass_number=1, force=False, extra_context=None):
                if stage_key == "04":
                    (task_dir_arg / CONTRACTS["04"].filename).write_text(valid_artifact("04"), encoding="utf-8")
                    if "04" not in state_arg["completed_stages"]:
                        state_arg["completed_stages"].append("04")
                    return EXIT_SUCCESS
                if stage_key == "04_gate":
                    calls["04_gate"] += 1
                    (task_dir_arg / CONTRACTS["04_gate"].filename).write_text(
                        gate_artifact(
                            "04_gate",
                            "ready_for_implementation: false\nblocking_issues: []\nnonblocking_issues: []\nrequired_revision_targets: []",
                        ),
                        encoding="utf-8",
                    )
                    if "04_gate" not in state_arg["completed_stages"]:
                        state_arg["completed_stages"].append("04_gate")
                    return EXIT_SUCCESS
                return EXIT_SUCCESS

            original = controller.ensure_real_stage
            controller.ensure_real_stage = fake_ensure
            self.addCleanup(lambda: setattr(controller, "ensure_real_stage", original))

            code = controller.run_stage4_gate_loop(task_dir, state, {"max_gate_passes": 2}, {})

            self.assertEqual(code, EXIT_BLOCKED)
            self.assertEqual(calls["04_gate"], 1)
            self.assertTrue((task_dir / "04_final_codex_brief.pass-1.md").exists())
            self.assertTrue((task_dir / "04_final_codex_brief.pass-2.md").exists())
            self.assertTrue((task_dir / "04_final_brief_audit.pass-1.md").exists())
            self.assertFalse((task_dir / "04_final_brief_audit.pass-2.md").exists())

    def test_stage4_retry_context_is_omitted_when_gate_file_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = "stage4-missing-gate-feedback"
            task_dir = Path(tmp) / task
            task_dir.mkdir(parents=True)
            state = new_state(task, "run-test")
            state["stage_gate_passes"] = [{"pass": 1, "accepted": False}]

            self.assertIsNone(controller.stage4_rejected_gate_context(task_dir, state, 2))

    def test_invoke_stage_appends_extra_context_and_completion_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "task"
            task_dir.mkdir(parents=True)
            state = new_state("task", "run-test")
            seen = {}
            original = controller.invoke_agent

            def fake_invoke_agent(task_dir_arg, config_arg, agent, stage_key, execution_mode, prompt_path, candidate_path, run_id, *args, **kwargs):
                seen["prompt"] = Path(prompt_path).read_text(encoding="utf-8")
                Path(candidate_path).parent.mkdir(parents=True, exist_ok=True)
                Path(candidate_path).write_text(valid_artifact(stage_key), encoding="utf-8")
                return {
                    "candidate_artifact_path": str(candidate_path),
                    "failure_class": None,
                    "exit_code": 0,
                }

            controller.invoke_agent = fake_invoke_agent
            original_source_snapshot = controller.source_snapshot
            self.addCleanup(lambda: setattr(controller, "invoke_agent", original))
            self.addCleanup(lambda: setattr(controller, "source_snapshot", original_source_snapshot))
            controller.source_snapshot = lambda: ""

            controller.invoke_stage(
                task_dir,
                state,
                {"usage_ledger": {"enabled": False}, "reasoning_capture": {"enabled": True}},
                "04",
                "read-only",
                "codex",
                2,
                completion_for="partial artifact",
                extra_context="gate feedback",
            )

            self.assertIn("gate feedback", seen["prompt"])
            self.assertIn("Complete the preserved partial artifact below", seen["prompt"])
            self.assertLess(seen["prompt"].index("gate feedback"), seen["prompt"].index("Complete the preserved partial artifact below"))

    def test_normalize_stage_output_strips_commentary_before_stage7_heading(self):
        output = "Provider note.\n\n# Stage 7 - Diff review\n\nBody.\n"

        self.assertEqual(
            controller.normalize_stage_output("07", output),
            "# Stage 7 - Diff review\n\nBody.\n",
        )

    def test_normalize_stage_output_strips_commentary_before_legacy_heading(self):
        output = "Provider note.\n\n# Stage 5 - Codex implementation report\n\nBody.\n"

        self.assertEqual(
            controller.normalize_stage_output("05", output),
            "# Stage 5 - Codex implementation report\n\nBody.\n",
        )

    def test_normalize_stage_output_returns_original_when_no_heading_matches(self):
        output = "Provider note.\n\nNo accepted artifact heading.\n"

        self.assertIs(controller.normalize_stage_output("07", output), output)
        self.assertIs(controller.normalize_stage_output("not_a_stage", output), output)

    def test_ensure_real_stage_success_path_has_single_unconditional_return(self):
        source = inspect.getsource(controller.ensure_real_stage)

        self.assertNotIn('result.get("failure_class") in (None, FAILURE_CLASS_MAX_TURNS, FAILURE_CLASS_UNKNOWN_FAILURE)', source)

    def test_stage4_prompt_uses_contract_section_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = prompts.prompt_text(Path(tmp) / "task", "task", "04")
        for section in CONTRACTS["04"].sections:
            self.assertIn("## " + section, text)
        # The extra sections beyond the contract's required 6 are kept as
        # explicitly optional guidance, not silently dropped (round-2 Stage
        # 7 review caught the earlier version of this fix regressing brief
        # quality by dropping them entirely).
        for section in ("Verification commands", "Stop conditions"):
            self.assertIn("## " + section, text)
        self.assertIn("not structurally required", text)

    def test_repository_analysis_budget_is_added_only_to_requested_stage_prompts(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "task"
            for stage_key in ("03", "04", "04_gate"):
                self.assertIn("Repository-analysis budget:", prompts.prompt_text(task_dir, "task", stage_key))
            self.assertNotIn("Repository-analysis budget:", prompts.prompt_text(task_dir, "task", "05"))

    def test_pipeline_verify_treats_none_duration_as_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.with_tasks_root(root)

            original = controller.verification.run_verification
            original_load_config = controller.load_config
            controller.load_config = lambda: {"verification": {"driven_project_commands": []}}
            controller.verification.run_verification = lambda *args, **kwargs: {
                "overall_status": "passed",
                "checks": [{"name": "unit", "status": "passed", "exit_code": 0, "duration_seconds": None}],
                "test_coverage_delta_signal": {"status": "not_checked"},
                "report_paths": {"md_path": str(root / "report.md")},
            }
            self.addCleanup(lambda: setattr(controller.verification, "run_verification", original))
            self.addCleanup(lambda: setattr(controller, "load_config", original_load_config))

            code, output = self.capture_verify("verify-none-duration")

            self.assertEqual(code, EXIT_SUCCESS)
            self.assertIn("unit: passed (exit=0, 0.0s)", output)
            self.assertIn("driven_project_checks_configured: false (0)", output)
            self.assertIn("driven_project_verified: false (unknown)", output)

    def test_pipeline_verify_loads_config_and_passes_driven_project_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.with_tasks_root(root)
            driven_commands = [{"name": "unit", "argv": ["true"]}]
            seen = {}

            original = controller.verification.run_verification
            original_load_config = controller.load_config
            controller.load_config = lambda: {"verification": {"driven_project_commands": driven_commands}}

            def fake_run_verification(*args, **kwargs):
                seen["driven_project_commands"] = kwargs.get("driven_project_commands")
                seen["allow_pid"] = kwargs.get("allow_pid")
                return {
                    "overall_status": "passed",
                    "checks": [],
                    "driven_project_checks_configured": True,
                    "driven_project_check_count": 1,
                    "driven_project_verified": True,
                    "driven_project_verification_reason": "all configured driven-project commands passed",
                    "test_coverage_delta_signal": {"status": "not_checked"},
                    "report_paths": {"md_path": str(root / "report.md")},
                }

            controller.verification.run_verification = fake_run_verification
            self.addCleanup(lambda: setattr(controller.verification, "run_verification", original))
            self.addCleanup(lambda: setattr(controller, "load_config", original_load_config))

            code, output = self.capture_verify("verify-config")

            self.assertEqual(code, EXIT_SUCCESS, output)
            self.assertIs(seen["driven_project_commands"], driven_commands)
            self.assertEqual(seen["allow_pid"], os.getpid())
            self.assertIn("driven_project_verified: true (all configured driven-project commands passed)", output)

    def test_pipeline_verify_passes_verification_toggles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.with_tasks_root(root)
            seen = {}

            original = controller.verification.run_verification
            original_load_config = controller.load_config
            controller.load_config = lambda: {"verification": {"driven_project_commands": [], "skip_self_check": True, "build_implies_compile": True}}

            def fake_run_verification(*args, **kwargs):
                seen.update(kwargs)
                return {
                    "overall_status": "passed",
                    "checks": [],
                    "test_coverage_delta_signal": {"status": "not_checked"},
                    "report_paths": {"md_path": str(root / "report.md")},
                }

            controller.verification.run_verification = fake_run_verification
            self.addCleanup(lambda: setattr(controller.verification, "run_verification", original))
            self.addCleanup(lambda: setattr(controller, "load_config", original_load_config))

            code, output = self.capture_verify("verify-toggles")

            self.assertEqual(code, EXIT_SUCCESS, output)
            self.assertTrue(seen["skip_self_check"])
            self.assertTrue(seen["build_implies_compile"])
            self.assertEqual(seen["allow_pid"], os.getpid())

    def test_pipeline_verify_refuses_existing_live_task_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.with_tasks_root(root)
            task_dir = root / "verify-locked"
            (task_dir / ".orchestrator").mkdir(parents=True)
            (task_dir / ".orchestrator" / "lock.json").write_text(
                '{"pid": %d, "host": "%s", "command": "run", "run_id": "other"}\n' % (os.getpid(), controller.socket.gethostname()),
                encoding="utf-8",
            )
            original_load_config = controller.load_config
            controller.load_config = lambda: {"verification": {"driven_project_commands": []}}
            self.addCleanup(lambda: setattr(controller, "load_config", original_load_config))

            code, output = self.capture_verify("verify-locked")

            self.assertEqual(code, controller.EXIT_LOCKED)
            self.assertIn("lock is active", output)

    def test_pipeline_usage_prints_cache_hit_for_groups_and_overall(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.with_usage_root(root / "usage")
            usage.append_entry(
                controller.usage_ledger_path(),
                usage.build_entry("t", "r", "02", "codex", {"duration_seconds": 1.0, "failure_class": None}, {"input_tokens": 25, "cache_read_tokens": 75, "total_cost_usd_estimated": 0.0001}),
            )

            code, output = self.capture_usage()

            self.assertEqual(code, EXIT_SUCCESS)
            self.assertIn("codex: calls=1 failures=0 duration=1.0s in=25 out=0 cost=unknown cost_estimated=$0.0001 cache_hit=75.0%", output)
            self.assertIn("overall: calls=1 failures=0 duration=1.0s in=25 out=0 cost=unknown cost_estimated=$0.0001 cache_hit=75.0%", output)

    def test_pipeline_usage_prints_known_real_cost_with_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.with_usage_root(root / "usage")
            usage.append_entry(
                controller.usage_ledger_path(),
                usage.build_entry("t", "r", "07", "claude", {"duration_seconds": 1.0, "failure_class": None}, {"input_tokens": 25, "output_tokens": 5, "total_cost_usd": 0.0001}),
            )

            code, output = self.capture_usage()

            self.assertEqual(code, EXIT_SUCCESS)
            self.assertIn("claude: calls=1 failures=0 duration=1.0s in=25 out=5 cost=$0.0001 cost_estimated=unknown", output)
            self.assertIn("overall: calls=1 failures=0 duration=1.0s in=25 out=5 cost=$0.0001 cost_estimated=unknown", output)

    def test_pipeline_usage_prints_unknown_estimated_cost_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.with_usage_root(root / "usage")
            usage.append_entry(
                controller.usage_ledger_path(),
                usage.build_entry("t", "r", "02", "codex", {"duration_seconds": 1.0, "failure_class": None}, {"input_tokens": 25}),
            )

            code, output = self.capture_usage()

            self.assertEqual(code, EXIT_SUCCESS)
            self.assertIn("cost_estimated=unknown", output)
            self.assertNotIn("cost_estimated=$0.0000", output)

    def test_pipeline_verify_reports_invalid_config_like_pipeline_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.with_tasks_root(root)

            original_load_config = controller.load_config
            controller.load_config = lambda: (_ for _ in ()).throw(controller.ConfigError("bad verification"))
            self.addCleanup(lambda: setattr(controller, "load_config", original_load_config))

            code, output = self.capture_verify("verify-invalid-config")

            self.assertEqual(code, EXIT_VALIDATION)
            self.assertIn("invalid real-run config: bad verification", output)

    def test_launch_background_spawns_module_cli_with_shared_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.with_tasks_root(root)
            calls = []

            class FakePopen(object):
                def __init__(self, argv, **kwargs):
                    self.pid = 4321
                    calls.append((argv, kwargs))

                def wait(self):
                    raise AssertionError("wait must not be called")

                def communicate(self):
                    raise AssertionError("communicate must not be called")

            original_popen = controller.subprocess.Popen
            original_executable = controller.sys.executable
            original_repo_root = controller.REPO_ROOT
            controller.subprocess.Popen = FakePopen
            controller.sys.executable = "/fake/python"
            controller.REPO_ROOT = root / "repo"
            self.addCleanup(lambda: setattr(controller.subprocess, "Popen", original_popen))
            self.addCleanup(lambda: setattr(controller.sys, "executable", original_executable))
            self.addCleanup(lambda: setattr(controller, "REPO_ROOT", original_repo_root))

            output = io.StringIO()
            with redirect_stdout(output):
                code = controller.launch_background("bg-task", ["run", "bg-task"], "background_run.log")

            self.assertEqual(code, EXIT_SUCCESS)
            self.assertEqual(len(calls), 1)
            argv, kwargs = calls[0]
            self.assertEqual(argv, ["/fake/python", "-m", "agent_pipeline.cli", "run", "bg-task"])
            self.assertEqual(kwargs["stdin"], controller.subprocess.DEVNULL)
            self.assertIs(kwargs["stdout"], kwargs["stderr"])
            self.assertEqual(kwargs["cwd"], str(root / "repo"))
            self.assertTrue(kwargs["start_new_session"])
            self.assertTrue((root / "bg-task" / ".orchestrator" / "background_run.log").exists())
            self.assertIn("child pid: 4321", output.getvalue())
            self.assertIn("background_run.log", output.getvalue())

    def test_pipeline_run_background_preserves_allow_dirty_and_prints_followups(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.with_tasks_root(root)
            calls = []

            original = controller.launch_background
            controller.launch_background = lambda task, argv_tail, log_name: calls.append((task, argv_tail, log_name)) or EXIT_SUCCESS
            self.addCleanup(lambda: setattr(controller, "launch_background", original))

            code, output = self.capture_run_background("bg-run", allow_dirty=True)

            self.assertEqual(code, EXIT_SUCCESS)
            self.assertEqual(calls, [("bg-run", ["run", "bg-run", "--allow-dirty"], "background_run.log")])
            self.assertNotIn("--background", calls[0][1])
            self.assertIn("catenna tail bg-run", output)
            self.assertIn("catenna status bg-run", output)
            self.assertIn("catenna report bg-run", output)

    def test_pipeline_verify_background_preserves_build_and_prints_tail_followup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.with_tasks_root(root)
            calls = []

            original = controller.launch_background
            controller.launch_background = lambda task, argv_tail, log_name: calls.append((task, argv_tail, log_name)) or EXIT_SUCCESS
            self.addCleanup(lambda: setattr(controller, "launch_background", original))

            code, output = self.capture_verify_background("bg-verify", run_build=True)

            self.assertEqual(code, EXIT_SUCCESS)
            self.assertEqual(calls, [("bg-verify", ["verify", "bg-verify", "--build"], "background_verify.log")])
            self.assertNotIn("--background", calls[0][1])
            self.assertIn(str(root / "bg-verify" / "05_verification_report.md"), output)
            self.assertIn("catenna tail bg-verify", output)
            self.assertIn(str(root / "bg-verify" / ".orchestrator" / "background_verify.log"), output)

    def test_status_prints_active_cross_task_cooldowns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.with_tasks_root(root)
            self.with_usage_root(root / "usage")
            usage.record_cooldown(controller.cooldown_store_path(), "codex", "usage_limit", None, "other-task", "run-1", 900)

            code, output = self.capture_status("status-cooldown-task")

            self.assertEqual(code, EXIT_SUCCESS)
            self.assertIn("cross_task_cooldowns:", output)
            self.assertIn("codex", output)

    def test_status_prints_nothing_extra_when_no_cooldown_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.with_tasks_root(root)
            self.with_usage_root(root / "usage")

            code, output = self.capture_status("status-no-cooldown-task")

            self.assertEqual(code, EXIT_SUCCESS)
            self.assertNotIn("cross_task_cooldowns:", output)

    def test_status_never_raises_on_corrupt_cooldown_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.with_tasks_root(root)
            usage_root = root / "usage"
            self.with_usage_root(usage_root)
            usage_root.mkdir(parents=True)
            (usage_root / "agent_cooldowns.json").write_text("not json", encoding="utf-8")

            code, output = self.capture_status("status-corrupt-cooldown-task")

            self.assertEqual(code, EXIT_SUCCESS)
            self.assertNotIn("cross_task_cooldowns:", output)


if __name__ == "__main__":
    unittest.main()
