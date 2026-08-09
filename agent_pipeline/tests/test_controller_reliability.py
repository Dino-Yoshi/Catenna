from __future__ import print_function

import io
import inspect
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from agent_pipeline import controller
from agent_pipeline import prompts
from agent_pipeline import usage
from agent_pipeline.failures import EXIT_BAD_INPUT, EXIT_BLOCKED, EXIT_SUCCESS, EXIT_VALIDATION
from agent_pipeline.mock_agent import gate_artifact, valid_artifact
from agent_pipeline.runner import atomic_finalize
from agent_pipeline.state import CONTRACTS, load_state, new_state, reconcile_artifacts, state_path, write_state_atomic


class ControllerReliabilityTests(unittest.TestCase):
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
        # Guards against .agent-pipeline/fixtures/mock_scenarios.json drifting
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

    def test_fresh_stage5_success_is_checkpointed_before_postprocessing(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = "stage5-checkpoint"
            task_dir = Path(tmp) / task
            task_dir.mkdir(parents=True)
            for stage_key in ("00", "01", "02", "03", "04", "04_gate"):
                (task_dir / CONTRACTS[stage_key].filename).write_text(valid_artifact(stage_key), encoding="utf-8")
            state = new_state(task, "run-test")

            def fake_ensure(task_dir_arg, state_arg, config_arg, stage_key, execution_mode, assignments, pass_number=1, force=False):
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

            def fake_ensure(task_dir_arg, state_arg, config_arg, stage_key, execution_mode, assignments, pass_number=1, force=False):
                calls.append((stage_key, pass_number, force))
                (task_dir_arg / CONTRACTS[stage_key].filename).write_text(valid_artifact(stage_key), encoding="utf-8")
                if stage_key not in state_arg["completed_stages"]:
                    state_arg["completed_stages"].append(stage_key)
                return EXIT_SUCCESS

            original = controller.ensure_real_stage
            controller.ensure_real_stage = fake_ensure
            self.addCleanup(lambda: setattr(controller, "ensure_real_stage", original))

            code = controller.run_stage4_gate_loop(task_dir, state, {"max_gate_passes": 2}, {})

            self.assertEqual(code, EXIT_SUCCESS)
            self.assertEqual(calls, [("04", 2, True), ("04_gate", 2, True)])

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

            def fake_ensure(task_dir_arg, state_arg, config_arg, stage_key, execution_mode, assignments, pass_number=1, force=False):
                calls.append((stage_key, pass_number, force))
                return EXIT_SUCCESS

            original = controller.ensure_real_stage
            controller.ensure_real_stage = fake_ensure
            self.addCleanup(lambda: setattr(controller, "ensure_real_stage", original))

            code = controller.run_stage4_gate_loop(task_dir, state, {"max_gate_passes": 1}, {})

            self.assertEqual(code, EXIT_BLOCKED)
            self.assertEqual(calls, [("04", 1, False), ("04_gate", 1, False)])

    def test_stage4_gate_loop_archives_brief_before_identical_revision_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = "stage4-archive-identical"
            task_dir = Path(tmp) / task
            task_dir.mkdir(parents=True)
            for stage_key in ("00", "01", "02", "03"):
                (task_dir / CONTRACTS[stage_key].filename).write_text(valid_artifact(stage_key), encoding="utf-8")
            state = new_state(task, "run-test")
            calls = {"04_gate": 0}

            def fake_ensure(task_dir_arg, state_arg, config_arg, stage_key, execution_mode, assignments, pass_number=1, force=False):
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
                return {
                    "overall_status": "passed",
                    "checks": [],
                    "test_coverage_delta_signal": {"status": "not_checked"},
                    "report_paths": {"md_path": str(root / "report.md")},
                }

            controller.verification.run_verification = fake_run_verification
            self.addCleanup(lambda: setattr(controller.verification, "run_verification", original))
            self.addCleanup(lambda: setattr(controller, "load_config", original_load_config))

            code, output = self.capture_verify("verify-config")

            self.assertEqual(code, EXIT_SUCCESS, output)
            self.assertIs(seen["driven_project_commands"], driven_commands)

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
