from __future__ import print_function

import json
import os
import socket
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from agent_pipeline import verification
from agent_pipeline.manifest import validate_manifest
from agent_pipeline.state import orchestrator_dir


PACKAGE_ROOT = Path(__file__).resolve().parents[2]


class ConcurrencyGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.task_dir = Path(self.tmp.name) / "task"
        self.task_dir.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write_lock(self, pid, host=None):
        directory = orchestrator_dir(self.task_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "lock.json"
        path.write_text(
            json.dumps({"pid": pid, "host": host or socket.gethostname(), "command": "pipeline-run"}),
            encoding="utf-8",
        )
        return path

    def test_no_lock_passes_silently(self):
        verification.check_concurrency_guard(self.task_dir)  # must not raise

    def test_live_pid_lock_is_refused(self):
        self.write_lock(os.getpid())
        with self.assertRaises(verification.VerificationError):
            verification.check_concurrency_guard(self.task_dir)

    def test_live_pid_lock_matching_allow_pid_passes(self):
        # run_real_pipeline calls run_verification from inside its own
        # already-held TaskLock; the guard must not treat that as a foreign
        # conflicting process.
        self.write_lock(os.getpid())
        verification.check_concurrency_guard(self.task_dir, allow_pid=os.getpid())  # must not raise

    def test_live_pid_lock_not_matching_allow_pid_is_refused(self):
        self.write_lock(os.getpid())
        with self.assertRaises(verification.VerificationError):
            verification.check_concurrency_guard(self.task_dir, allow_pid=os.getpid() + 1)

    def test_dead_pid_lock_passes(self):
        self.write_lock(2 ** 30)
        verification.check_concurrency_guard(self.task_dir)  # must not raise

    def test_unreadable_lock_is_refused(self):
        directory = orchestrator_dir(self.task_dir)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "lock.json").write_text("not json", encoding="utf-8")
        with self.assertRaises(verification.VerificationError):
            verification.check_concurrency_guard(self.task_dir)


class ParseUnittestSummaryTests(unittest.TestCase):
    def test_ok_output(self):
        text = "..........\n----------------------------------------------------------------------\nRan 83 tests in 2.099s\n\nOK\n"
        summary = verification.parse_unittest_summary(text)
        self.assertEqual(summary["tests_run"], 83)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["failures"], 0)
        self.assertEqual(summary["errors"], 0)

    def test_failed_output(self):
        text = (
            "======================================================================\n"
            "FAIL: test_x (module.Class)\n"
            "----------------------------------------------------------------------\n"
            "Ran 84 tests in 2.1s\n\n"
            "FAILED (failures=1, errors=2)\n"
        )
        summary = verification.parse_unittest_summary(text)
        self.assertEqual(summary["tests_run"], 84)
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["failures"], 1)
        self.assertEqual(summary["errors"], 2)

    def test_empty_output(self):
        summary = verification.parse_unittest_summary("")
        self.assertIsNone(summary["tests_run"])
        self.assertFalse(summary["ok"])


class TestCoverageDeltaSignalTests(unittest.TestCase):
    def test_no_changed_files_is_no_data(self):
        signal = verification.test_coverage_delta_signal({"changed_files": []})
        self.assertEqual(signal["status"], "no_data")

    def test_none_manifest_is_no_data(self):
        signal = verification.test_coverage_delta_signal(None)
        self.assertEqual(signal["status"], "no_data")

    def test_testable_source_without_tests_is_flagged(self):
        manifest = {"changed_files": [{"path": "src/main/java/com/example/Thing.java"}]}
        signal = verification.test_coverage_delta_signal(manifest)
        self.assertEqual(signal["status"], "flagged")
        self.assertEqual(signal["flagged_paths"], ["src/main/java/com/example/Thing.java"])
        self.assertFalse(signal["touched_test_files"])

    def test_testable_source_with_matching_test_change_is_ok(self):
        manifest = {
            "changed_files": [
                {"path": "src/main/java/com/example/Thing.java"},
                {"path": "src/test/java/com/example/ThingTest.java"},
            ]
        }
        signal = verification.test_coverage_delta_signal(manifest)
        self.assertEqual(signal["status"], "ok")
        self.assertTrue(signal["touched_test_files"])
        self.assertEqual(signal["flagged_paths"], [])

    def test_python_pipeline_change_with_matching_test_is_ok(self):
        manifest = {
            "changed_files": [
                {"path": "agent_pipeline/verification.py"},
                {"path": "agent_pipeline/tests/test_verification.py"},
            ]
        }
        signal = verification.test_coverage_delta_signal(manifest)
        self.assertEqual(signal["status"], "ok")

    def test_non_testable_paths_are_never_flagged(self):
        manifest = {"changed_files": [{"path": "docs/agent-pipeline/OVERVIEW.md"}, {"path": ".gitignore"}]}
        signal = verification.test_coverage_delta_signal(manifest)
        self.assertEqual(signal["status"], "ok")
        self.assertEqual(signal["testable_changed_paths"], [])


class UpdateManifestVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.task_dir = Path(self.tmp.name) / "task"
        self.task_dir.mkdir(parents=True)
        self.manifest = {
            "schema_version": 1,
            "task": "t",
            "stage": "05",
            "changed_files": [{"path": "src/main/java/com/example/Thing.java"}],
            "verification": {"unit_tests": "not_attempted", "mock_pipeline": "not_attempted", "diff_check": "not_attempted"},
            "verification_evidence": [],
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_fills_in_statuses_from_checks(self):
        checks = {"unit_tests": {"status": "passed"}, "mock_pipeline": {"status": "failed"}}
        signal = {"status": "ok", "note": "fine"}
        updated = verification.update_manifest_verification(self.task_dir, self.manifest, checks, signal)
        self.assertEqual(updated["verification"]["unit_tests"], "passed")
        self.assertEqual(updated["verification"]["mock_pipeline"], "failed")
        self.assertEqual(updated["verification"]["diff_check"], "passed")
        validate_manifest(updated)  # must still satisfy the manifest contract

    def test_flagged_signal_marks_diff_check_failed(self):
        checks = {"unit_tests": {"status": "passed"}, "mock_pipeline": {"status": "passed"}}
        signal = {"status": "flagged", "note": "missing tests"}
        updated = verification.update_manifest_verification(self.task_dir, self.manifest, checks, signal)
        self.assertEqual(updated["verification"]["diff_check"], "failed")

    def test_evidence_is_appended_not_replaced(self):
        self.manifest["verification_evidence"] = [{"check": "prior", "status": "passed"}]
        checks = {"unit_tests": {"status": "passed"}}
        signal = {"status": "ok", "note": "fine"}
        updated = verification.update_manifest_verification(self.task_dir, self.manifest, checks, signal)
        checks_seen = [item["check"] for item in updated["verification_evidence"]]
        self.assertIn("prior", checks_seen)
        self.assertIn("unit_tests", checks_seen)
        self.assertIn("diff_check", checks_seen)

    def test_writes_manifest_file_back_to_disk(self):
        path = self.task_dir / "05_implementation_manifest.json"
        path.write_text(json.dumps(self.manifest), encoding="utf-8")
        checks = {"unit_tests": {"status": "passed"}}
        signal = {"status": "ok", "note": "fine"}
        verification.update_manifest_verification(self.task_dir, self.manifest, checks, signal)
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["verification"]["unit_tests"], "passed")

    def test_none_manifest_is_a_noop(self):
        result = verification.update_manifest_verification(self.task_dir, None, {}, {"status": "no_data", "note": ""})
        self.assertIsNone(result)


class RunGradleFakeFixtureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.tmp.name)
        self.runs_dir = self.repo_root / "runs"
        self.runs_dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write_fake_gradlew(self, body):
        path = self.repo_root / "gradlew"
        path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body), encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_passes_no_daemon_and_task_name(self):
        self.write_fake_gradlew(
            """
            import sys
            assert "--no-daemon" in sys.argv
            assert "compileJava" in sys.argv
            """
        )
        result = verification.run_gradle(self.repo_root, self.runs_dir, "compileJava")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["exit_code"], 0)

    def test_sets_java_home_and_gradle_user_home_env(self):
        self.write_fake_gradlew(
            """
            import os
            assert os.environ.get("JAVA_HOME") == "/usr/lib/jvm/java-8-openjdk-amd64"
            assert os.environ.get("GRADLE_USER_HOME", "").endswith(".gradle-user-home")
            """
        )
        result = verification.run_gradle(self.repo_root, self.runs_dir, "compileJava")
        self.assertEqual(result["status"], "passed")

    def test_nonzero_exit_is_failed_status(self):
        self.write_fake_gradlew(
            """
            import sys
            sys.exit(1)
            """
        )
        result = verification.run_gradle(self.repo_root, self.runs_dir, "compileJava")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 1)

    def test_missing_gradlew_is_not_attempted(self):
        result = verification.run_gradle(self.repo_root, self.runs_dir, "compileJava")
        self.assertEqual(result["status"], "not_attempted")


class RunUnitTestsAndMockPipelineIntegrationTests(unittest.TestCase):
    """Real (not faked) invocations against this actual repo -- both
    commands are fast and fully in-process/deterministic (no real agent
    CLI calls), so exercising them for real is cheap and catches argv/env
    plumbing mistakes a fake fixture could paper over."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runs_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_mock_pipeline_reports_real_exit_status(self):
        # `python3 -m agent_pipeline.cli mock-test` used to fail
        # deterministically against stale fixture expectations in
        # .agent-pipeline/fixtures/mock_scenarios.json (pre-existing,
        # confirmed unrelated to Phase 2 -- see phase-2-handoff.md "Known
        # gaps"); fixed in Phase 3 (see phase-3-handoff.md). This asserts
        # run_mock_pipeline faithfully reports the real (now passing) result.
        result = verification.run_mock_pipeline(PACKAGE_ROOT, self.runs_dir, timeout_seconds=60)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["exit_code"], 0)


class RunVerificationOrchestrationTests(unittest.TestCase):
    """Exercises run_verification's control flow (guard -> checks ->
    manifest update -> report write) with fast fake gradlew/python stand-ins
    so this test doesn't pay for a real unittest-discover or gradle run."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.tmp.name) / "repo"
        self.repo_root.mkdir(parents=True)
        self.task_dir = self.repo_root / ".agent-pipeline" / "tasks" / "t"
        self.task_dir.mkdir(parents=True)
        fake_python = self.repo_root / "fake_python.py"
        fake_python.write_text(
            "#!/usr/bin/env python3\nimport sys\nsys.stderr.write('Ran 3 tests in 0.1s\\n\\nOK\\n')\nsys.exit(0)\n",
            encoding="utf-8",
        )
        fake_python.chmod(0o755)
        gradlew = self.repo_root / "gradlew"
        gradlew.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n", encoding="utf-8")
        gradlew.chmod(0o755)

        self._orig_python_executable = verification.python_executable
        self._orig_unit_test_args = verification.UNIT_TEST_ARGS
        self._orig_mock_pipeline_args = verification.MOCK_PIPELINE_ARGS
        verification.python_executable = lambda: str(fake_python)
        verification.UNIT_TEST_ARGS = []
        verification.MOCK_PIPELINE_ARGS = []

    def tearDown(self):
        verification.python_executable = self._orig_python_executable
        verification.UNIT_TEST_ARGS = self._orig_unit_test_args
        verification.MOCK_PIPELINE_ARGS = self._orig_mock_pipeline_args
        self.tmp.cleanup()

    def test_full_run_writes_report_and_updates_manifest(self):
        manifest = {
            "schema_version": 1,
            "task": "t",
            "stage": "05",
            "changed_files": [{"path": "src/main/java/com/example/Thing.java"}],
            "verification": {"unit_tests": "not_attempted", "mock_pipeline": "not_attempted", "diff_check": "not_attempted"},
            "verification_evidence": [],
        }
        (self.task_dir / "05_implementation_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        report = verification.run_verification(self.task_dir, self.repo_root)

        self.assertEqual(report["overall_status"], "passed")
        self.assertTrue(report["manifest_present"])
        self.assertTrue(report["manifest_updated"])
        self.assertEqual(report["test_coverage_delta_signal"]["status"], "flagged")

        json_path = self.task_dir / "05_verification_report.json"
        md_path = self.task_dir / "05_verification_report.md"
        self.assertTrue(json_path.exists())
        self.assertTrue(md_path.exists())
        on_disk = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["overall_status"], "passed")

        updated_manifest = json.loads((self.task_dir / "05_implementation_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(updated_manifest["verification"]["unit_tests"], "passed")
        self.assertEqual(updated_manifest["verification"]["mock_pipeline"], "passed")
        self.assertEqual(updated_manifest["verification"]["diff_check"], "failed")

    def test_run_without_manifest_still_produces_a_report(self):
        report = verification.run_verification(self.task_dir, self.repo_root)
        self.assertFalse(report["manifest_present"])
        self.assertFalse(report["manifest_updated"])
        self.assertEqual(report["test_coverage_delta_signal"]["status"], "no_data")

    def test_concurrency_guard_blocks_run_verification(self):
        directory = orchestrator_dir(self.task_dir)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "lock.json").write_text(
            json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "command": "pipeline-run"}),
            encoding="utf-8",
        )
        with self.assertRaises(verification.VerificationError):
            verification.run_verification(self.task_dir, self.repo_root)


if __name__ == "__main__":
    unittest.main()
