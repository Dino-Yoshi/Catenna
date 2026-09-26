"""R04 regressions: interruption stops the current controller invocation."""

from __future__ import print_function

import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from agent_pipeline import controller, verification
from agent_pipeline.failures import EXIT_INTERRUPTED, FAILURE_CLASS_PROCESS_INTERRUPTED
from agent_pipeline.real_runner import ManagedProcessInterrupted
from agent_pipeline.state import CONTRACTS, load_state, orchestrator_dir
from agent_pipeline.tests import test_real_pipeline as base


class R04RealCheckInterruptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.runs = self.root / "runs"
        self.runs.mkdir()

    def script(self, name, body):
        path = self.repo / name
        path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def process_running(self, pid):
        try:
            text = Path("/proc/%s/stat" % pid).read_text(encoding="utf-8")
            return text[text.rfind(")") + 2:].split()[0] != "Z"
        except FileNotFoundError:
            return False

    def test_r04_fr1_fr2_fr3_sigint_and_sigterm_stop_remaining_checks(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum):
                pid_path = self.root / ("child-%s.pid" % int(signum))
                second_path = self.root / ("second-%s.started" % int(signum))
                result_path = self.root / ("result-%s.json" % int(signum))
                task_dir = self.repo / ".agent-pipeline" / "tasks" / ("signal-%s" % int(signum))
                task_dir.mkdir(parents=True)
                first = self.script(
                    "first-%s.py" % int(signum),
                    "import os, sys, time\nopen(sys.argv[1], 'w').write(str(os.getpid()))\ntime.sleep(60)\n",
                )
                second = self.script(
                    "second-%s.py" % int(signum),
                    "import pathlib, sys\npathlib.Path(sys.argv[1]).write_text('started')\n",
                )
                commands = [
                    {"name": "first", "argv": [str(first), str(pid_path)]},
                    {"name": "second", "argv": [str(second), str(second_path)]},
                ]
                worker_code = (
                    "import json, sys\n"
                    "from pathlib import Path\n"
                    "from agent_pipeline import verification\n"
                    "from agent_pipeline.real_runner import ManagedProcessInterrupted\n"
                    "try:\n"
                    "    verification.run_verification(Path(%r), Path(%r), skip_self_check=True, driven_project_commands=%r)\n"
                    "except ManagedProcessInterrupted as exc:\n"
                    "    Path(%r).write_text(json.dumps(exc.report), encoding='utf-8')\n"
                    "    sys.exit(130)\n"
                ) % (str(task_dir), str(self.repo), commands, str(result_path))
                worker = subprocess.Popen([sys.executable, "-c", worker_code])
                try:
                    deadline = time.time() + 5
                    while time.time() < deadline and not pid_path.exists():
                        time.sleep(0.02)
                    self.assertTrue(pid_path.exists(), "first verification check did not launch")
                    child_pid = int(pid_path.read_text(encoding="utf-8"))
                    worker.send_signal(signum)
                    worker.wait(timeout=10)
                    self.assertEqual(worker.returncode, EXIT_INTERRUPTED)
                    report = json.loads(result_path.read_text(encoding="utf-8"))
                    checks = [item for item in report["checks"] if item["name"].startswith("driven_project_")]
                    self.assertEqual(report["overall_status"], "interrupted")
                    self.assertEqual([item["status"] for item in checks], ["interrupted", "not_attempted"])
                    persisted = json.loads((task_dir / "05_verification_report.json").read_text(encoding="utf-8"))
                    self.assertEqual(persisted["overall_status"], "interrupted")
                    self.assertFalse(second_path.exists(), "second check launched after interruption")
                    deadline = time.time() + 5
                    while time.time() < deadline and self.process_running(child_pid):
                        time.sleep(0.05)
                    self.assertFalse(self.process_running(child_pid), "managed check survived interruption")
                finally:
                    if worker.poll() is None:
                        worker.kill()
                        worker.wait()

    def test_r04_fr4_timeout_is_failure_and_does_not_interrupt_invocation(self):
        second_path = self.root / "second.started"
        slow = self.script("slow.py", "import time\ntime.sleep(2)\n")
        second = self.script("second.py", "import pathlib, sys\npathlib.Path(sys.argv[1]).write_text('started')\n")
        checks = verification.run_driven_project_checks(
            self.repo,
            self.runs,
            [
                {"name": "slow", "argv": [str(slow)], "timeout_seconds": 0.05},
                {"name": "second", "argv": [str(second), str(second_path)]},
            ],
        )
        self.assertEqual(checks[0]["status"], "failed")
        self.assertTrue(checks[0]["timed_out"])
        self.assertEqual(checks[1]["status"], "passed")
        self.assertTrue(second_path.exists())


class R04ControllerInterruptionTests(unittest.TestCase):
    setUp = base.RealPipelineTests.setUp
    tearDown = base.RealPipelineTests.tearDown
    config = base.RealPipelineTests.config
    write_fake_agent = base.RealPipelineTests.write_fake_agent
    verification_report = base.RealPipelineTests.verification_report

    def pipeline(self):
        with redirect_stdout(io.StringIO()):
            return controller.pipeline_run(self.task, allow_dirty=True)

    def test_r04_fr2_fr3_interrupted_verification_blocks_overseer_and_exits_130(self):
        report = self.verification_report(overall_status="interrupted", coverage_status="ok", driven_project_verified=False)
        report["checks"] = [
            {"name": "driven_project_first", "status": "interrupted"},
            {"name": "driven_project_second", "status": "not_attempted"},
        ]

        def interrupt(*args, **kwargs):
            exc = ManagedProcessInterrupted(signal.SIGINT)
            exc.report = report
            raise exc

        controller.verification.run_verification = interrupt
        with mock.patch.object(controller, "run_overseer_or_fallback") as overseer:
            self.assertEqual(self.pipeline(), EXIT_INTERRUPTED)
            overseer.assert_not_called()

        state = load_state(self.task_dir, self.task)
        self.assertEqual(state["state"], "blocked")
        self.assertEqual(state["last_failure"]["failure_class"], FAILURE_CLASS_PROCESS_INTERRUPTED)
        self.assertEqual(state["evidence"]["verification"][-1]["status"], "interrupted")
        self.assertFalse((self.task_dir / CONTRACTS["06"].filename).exists())

    def test_r04_fr3_interrupted_agent_attempt_is_durable_and_exits_130(self):
        original = controller.invoke_agent

        def interrupt_agent(task_dir, config, agent, stage_key, execution_mode, prompt_path, candidate_path, run_id, *args, **kwargs):
            candidate_path = Path(candidate_path)
            candidate_path.parent.mkdir(parents=True, exist_ok=True)
            candidate_path.write_text("partial output\n", encoding="utf-8")
            metadata_path = candidate_path.with_suffix(".json")
            stdout_path = candidate_path.with_suffix(".stdout")
            stderr_path = candidate_path.with_suffix(".stderr")
            stdout_path.write_text("partial output\n", encoding="utf-8")
            stderr_path.write_text("", encoding="utf-8")
            result = {
                "agent": agent,
                "stage": stage_key,
                "execution_mode": execution_mode,
                "run_id": run_id,
                "status": "interrupted",
                "exit_code": -int(signal.SIGTERM),
                "failure_class": FAILURE_CLASS_PROCESS_INTERRUPTED,
                "partial": True,
                "candidate_artifact_path": str(candidate_path),
                "metadata_path": str(metadata_path),
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
            }
            metadata_path.write_text(json.dumps(result), encoding="utf-8")
            exc = ManagedProcessInterrupted(signal.SIGTERM)
            exc.result = result
            raise exc

        controller.invoke_agent = interrupt_agent
        self.addCleanup(setattr, controller, "invoke_agent", original)

        self.assertEqual(self.pipeline(), EXIT_INTERRUPTED)
        state = load_state(self.task_dir, self.task)
        attempt = state["real_stage_runs"]["02"][-1]
        self.assertEqual(attempt["status"], "interrupted")
        completed = json.loads((orchestrator_dir(self.task_dir) / "attempts" / attempt["attempt_id"] / "completed.json").read_text(encoding="utf-8"))
        self.assertEqual(completed["status"], "interrupted")
        self.assertFalse(completed["eligible_for_promotion"])
        self.assertEqual(completed["result"]["status"], "interrupted")

    def test_r04_fr3_invalid_evidence_cannot_auto_accept(self):
        report = self.verification_report(overall_status="interrupted", coverage_status="ok", driven_project_verified=False)
        report["checks"] = [{"name": "driven_project_fixture", "status": "interrupted"}]
        eligibility = controller.automatic_verification_eligibility(self.config(), report, {"route": "manual_test"})
        self.assertFalse(eligibility["eligible"])

    def test_r04_fr1_fr3_valid_automatic_acceptance_path_is_preserved(self):
        controller.verification.run_verification = lambda *args, **kwargs: self.verification_report(
            overall_status="passed", coverage_status="ok", driven_project_verified=True
        )
        self.assertEqual(self.pipeline(), 0)
        state = load_state(self.task_dir, self.task)
        self.assertEqual(state["evidence"]["verification"][-1]["status"], "passed")
        self.assertEqual(state["evidence"]["stage06"][-1]["route"], "auto")


if __name__ == "__main__":
    unittest.main()
