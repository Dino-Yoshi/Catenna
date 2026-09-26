"""R06 regressions: execution ownership survives every child boundary."""

from __future__ import print_function

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_pipeline import controller, verification
from agent_pipeline.failures import EXIT_BLOCKED, EXIT_SUCCESS
from agent_pipeline.locking import ExecutionOwnership, LockError, lock_path, worktree_lock_path
from agent_pipeline.mock_agent import valid_artifact
from agent_pipeline.state import CONTRACTS, load_state, new_state
from agent_pipeline.tests import test_real_pipeline as base


class R06AgentOwnershipTests(unittest.TestCase):
    setUp = base.RealPipelineTests.setUp
    tearDown = base.RealPipelineTests.tearDown
    config = base.RealPipelineTests.config
    write_fake_agent = base.RealPipelineTests.write_fake_agent
    verification_report = base.RealPipelineTests.verification_report
    stage_result = base.RealPipelineTests.stage_result

    def _run_tampering_attempt(self, record_name, replacement):
        run_id = "r06-agent-run"
        state = new_state(self.task, run_id)
        target = lock_path(self.task_dir) if record_name == "task lock" else worktree_lock_path(self.root)
        sabotage = self.root / "r06_sabotage_agent.py"
        mutation = "path.unlink()" if replacement is None else "path.write_text(%r, encoding='utf-8')" % replacement
        sabotage.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            "path = pathlib.Path(%r)\n" % str(target)
            + mutation
            + "\noutput = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])\n"
            + "output.write_text(%r, encoding='utf-8')\n" % valid_artifact("02"),
            encoding="utf-8",
        )
        sabotage.chmod(0o755)
        cfg = self.config()
        cfg["agents"]["codex"]["command"] = str(sabotage)

        code = None
        try:
            try:
                with ExecutionOwnership(self.task_dir, self.root, "run", run_id, self.task):
                    code = controller.ensure_real_stage(
                        self.task_dir, state, cfg, "02", "read-only", {}
                    )
            except LockError:
                # An unreadable replacement also prevents the normal unlock;
                # the postcondition result below remains the primary evidence.
                pass
        finally:
            for path in (lock_path(self.task_dir), worktree_lock_path(self.root)):
                if path.exists():
                    path.unlink()

        self.assertEqual(code, EXIT_BLOCKED)
        self.assertFalse((self.task_dir / CONTRACTS["02"].filename).exists())
        postcondition = state["real_stage_runs"]["02"][-1]["postcondition"]
        self.assertFalse(postcondition["valid"])
        self.assertIn(record_name, postcondition["ownership_error"])
        self.assertIn(record_name, state["last_failure"]["reason"])

    def test_r06_fr1_fr2_missing_unreadable_and_replaced_records_block_promotion(self):
        cases = (
            ("task lock", None),
            ("task lock", "not json\n"),
            ("task lock", json.dumps({"host": "other", "run_id": "other", "pid": 1}) + "\n"),
            ("worktree execution lock", None),
            ("worktree execution lock", "not json\n"),
            ("worktree execution lock", json.dumps({"host": "other", "run_id": "other", "pid": 1}) + "\n"),
        )
        for index, (record_name, replacement) in enumerate(cases):
            if index:
                # Each case needs its own task directory: durable attempt
                # records from an earlier case would otherwise block recovery.
                self.tearDown()
                self.setUp()
            with self.subTest(record=record_name, replacement=replacement):
                self._run_tampering_attempt(record_name, replacement)

    def test_r06_fr3_managed_child_updates_preserve_valid_automatic_acceptance(self):
        controller.verification.run_verification = lambda *args, **kwargs: self.verification_report(
            overall_status="passed", coverage_status="ok", driven_project_verified=True
        )
        code = controller.pipeline_run(self.task, allow_dirty=True)
        self.assertEqual(code, EXIT_SUCCESS)
        state = load_state(self.task_dir, self.task)
        self.assertEqual(state["state"], "complete")
        runs = [
            run
            for stage_runs in state["real_stage_runs"].values()
            for run in stage_runs
            if run.get("provider") != "controller"
        ]
        self.assertTrue(runs)
        self.assertTrue(all(run["postcondition"]["valid"] for run in runs))
        self.assertTrue(all(run["postcondition"]["ownership_error"] is None for run in runs))

    def test_r06_fr2_verification_ownership_failure_does_not_dispatch_overseer(self):
        state = new_state(self.task, "r06-verification-controller")
        original_verification = controller.run_bound_verification
        original_overseer = controller.run_overseer_or_fallback
        overseer_calls = []
        controller.run_bound_verification = lambda *args, **kwargs: (
            None,
            {"ownership_failure": True},
            "worktree execution lock /tmp/execution-lock.json is missing",
        )
        controller.run_overseer_or_fallback = lambda *args, **kwargs: overseer_calls.append(args)
        try:
            code = controller.run_stage6_transition(
                self.task_dir, state, self.config(), {}, {"changed_files": []}
            )
        finally:
            controller.run_bound_verification = original_verification
            controller.run_overseer_or_fallback = original_overseer
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(overseer_calls, [])
        self.assertIn("worktree execution lock", state["last_failure"]["reason"])


class R06VerificationOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        subprocess.check_call(
            ["git", "init"], cwd=str(self.repo), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self.task_dir = self.repo / ".agent-pipeline" / "tasks" / "r06-verify"
        self.task_dir.mkdir(parents=True)
        self.second_started = self.repo / "second.started"
        self.sabotage = self.repo / "sabotage.py"
        self.sabotage.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            "path = pathlib.Path(sys.argv[1])\n"
            "if sys.argv[2] == 'delete':\n"
            "    path.unlink()\n"
            "else:\n"
            "    path.write_text('{\\\"host\\\": \\\"replacement\\\", \\\"run_id\\\": \\\"wrong\\\", \\\"pid\\\": 1}\\n')\n",
            encoding="utf-8",
        )
        self.sabotage.chmod(0o755)
        self.second = self.repo / "second.py"
        self.second.write_text(
            "#!/usr/bin/env python3\nimport pathlib, sys\npathlib.Path(sys.argv[1]).write_text('started')\n",
            encoding="utf-8",
        )
        self.second.chmod(0o755)

    def _assert_check_tampering_stops_plan(self, record_name, action):
        run_id = "r06-verification-run"
        target = lock_path(self.task_dir) if record_name == "task lock" else worktree_lock_path(self.repo)
        commands = [
            {"name": "tamper", "argv": [str(self.sabotage), str(target), action]},
            {"name": "second", "argv": [str(self.second), str(self.second_started)]},
        ]
        try:
            with ExecutionOwnership(self.task_dir, self.repo, "verify", run_id, self.task_dir.name):
                with self.assertRaises(verification.VerificationError) as raised:
                    verification.run_verification(
                        self.task_dir,
                        self.repo,
                        skip_self_check=True,
                        driven_project_commands=commands,
                        run_id=run_id,
                        allow_pid=os.getpid(),
                    )
        finally:
            for path in (lock_path(self.task_dir), worktree_lock_path(self.repo)):
                if path.exists():
                    path.unlink()
        self.assertIn(record_name, str(raised.exception))
        self.assertFalse(self.second_started.exists(), "verification continued after ownership loss")

    def test_r06_fr1_fr2_each_verification_check_validates_both_records(self):
        for record_name, action in (
            ("task lock", "delete"),
            ("worktree execution lock", "replace"),
        ):
            with self.subTest(record=record_name, action=action):
                if self.second_started.exists():
                    self.second_started.unlink()
                self._assert_check_tampering_stops_plan(record_name, action)


if __name__ == "__main__":
    unittest.main()
