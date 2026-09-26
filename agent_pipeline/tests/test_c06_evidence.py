"""C06 regressions: verification and review bound to source identity (A3).

These drive the real controller against a real temporary Git worktree with
fake agent CLIs; only the verification command runner is faked, and the
source-identity capture is the real implementation.
"""

from __future__ import print_function

import io
import json
import os
import shutil
import subprocess
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from agent_pipeline import controller
from agent_pipeline import evidence
from agent_pipeline.artifacts import sha256_file
from agent_pipeline.failures import EXIT_BLOCKED, EXIT_SUCCESS, EXIT_VALIDATION
from agent_pipeline.real_runner import ManagedProcessInterrupted
from agent_pipeline.source_identity import SourceIdentityError
from agent_pipeline.state import CONTRACTS, load_state, orchestrator_dir, write_state_atomic
from agent_pipeline.tests import test_real_pipeline as base


class C06EvidenceTests(unittest.TestCase):
    # Reuse the real-pipeline fixture without re-collecting its tests.
    setUp = base.RealPipelineTests.setUp
    tearDown = base.RealPipelineTests.tearDown
    config = base.RealPipelineTests.config
    write_fake_agent = base.RealPipelineTests.write_fake_agent
    verification_report = base.RealPipelineTests.verification_report
    manual_notes = base.RealPipelineTests.manual_notes
    setUp_for_second_task = base.RealPipelineTests.setUp_for_second_task

    # ---- helpers -------------------------------------------------------

    def start(self):
        self.count_path = self.root / "counts.txt"
        os.environ["FAKE_COUNT_PATH"] = str(self.count_path)
        self.addCleanup(lambda: os.environ.pop("FAKE_COUNT_PATH", None))
        self.verification_calls = []
        self.verification_outcome = "passed"
        self.during_verification = None

        def fake_run_verification(*args, **kwargs):
            self.verification_calls.append(kwargs)
            if self.during_verification is not None:
                self.during_verification()
            if self.verification_outcome == "interrupt":
                raise ManagedProcessInterrupted(2)
            if self.verification_outcome == "passed":
                report = self.verification_report(overall_status="passed", coverage_status="ok", driven_project_verified=True)
            else:
                report = self.verification_report(overall_status="failed", coverage_status="ok", driven_project_verified=False)
            report["report_paths"] = {"md_path": str(self.task_dir / "05_verification_report.md")}
            return report

        controller.verification.run_verification = fake_run_verification

    def git(self, *args):
        subprocess.check_call(
            ["git", "-c", "user.name=Catenna Test", "-c", "user.email=test@example.invalid"] + list(args),
            cwd=str(self.root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def dispatches(self, stage):
        if not self.count_path.exists():
            return 0
        return self.count_path.read_text(encoding="utf-8").splitlines().count(stage)

    def state(self):
        return load_state(self.task_dir, self.task)

    def identity(self):
        identity, error = controller.current_source_identity(self.task_dir)
        self.assertIsNone(error)
        return identity

    def status_output(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = controller.status(self.task)
        self.assertEqual(code, EXIT_SUCCESS)
        return out.getvalue()

    def dry_run_output(self):
        out = io.StringIO()
        with redirect_stdout(out):
            controller.dry_run(self.task)
        return out.getvalue()

    def pipeline(self):
        out = io.StringIO()
        with redirect_stdout(out):
            return controller.pipeline_run(self.task, allow_dirty=True)

    def accept(self):
        code = self.pipeline()
        self.assertEqual(code, EXIT_SUCCESS)
        self.assertIn("current_acceptance: yes", self.status_output())
        return self.state()

    def history_files(self):
        root = orchestrator_dir(self.task_dir) / "history"
        if not root.exists():
            return []
        return sorted(str(path.relative_to(root)).split("/", 1)[1] for path in root.glob("*/*") if path.name != "reason.json")

    def write_notes(self, text):
        (self.task_dir / CONTRACTS["06"].filename).write_text(text, encoding="utf-8")

    # ---- C06-FR1 / valid automatic path ------------------------------

    def test_fr1_fr2_valid_automatic_acceptance_records_bound_evidence(self):
        self.start()
        state = self.accept()
        identity = self.identity()
        records = state["evidence"]

        verification = records["verification"][-1]
        self.assertEqual(verification["status"], "passed")
        self.assertTrue(verification["source_current"])
        self.assertEqual(verification["source_before"]["fingerprint"], identity["fingerprint"])
        self.assertEqual(verification["source_after"]["fingerprint"], identity["fingerprint"])
        self.assertEqual(verification["inputs"]["brief"], sha256_file(self.task_dir / CONTRACTS["04"].filename))
        self.assertEqual(verification["inputs"]["gate"], sha256_file(self.task_dir / CONTRACTS["04_gate"].filename))
        self.assertEqual(verification["inputs"]["verification_config"], evidence.verification_config_hash(self.config()))

        stage06 = records["stage06"][-1]
        self.assertEqual(stage06["route"], "auto")
        self.assertEqual(stage06["verification_record_id"], verification["record_id"])
        self.assertEqual(stage06["source"]["fingerprint"], identity["fingerprint"])
        self.assertIn(controller.format_identity(identity), (self.task_dir / CONTRACTS["06"].filename).read_text(encoding="utf-8"))

        stage07 = records["stage07"][-1]
        self.assertEqual(stage07["status"], "passed")
        self.assertEqual(stage07["stage06_record_id"], stage06["record_id"])
        self.assertEqual(stage07["inputs"]["review_config"], evidence.review_config_hash(self.config()))
        self.assertEqual(stage07["artifact_hash"], sha256_file(self.task_dir / CONTRACTS["07"].filename))

        stage08 = records["stage08"][-1]
        self.assertEqual(stage08["decision"], "accept")
        self.assertEqual(stage08["stage07_record_id"], stage07["record_id"])
        self.assertEqual(stage08["source"]["fingerprint"], identity["fingerprint"])

        status = self.status_output()
        self.assertIn("source_identity: " + controller.format_identity(identity), status)
        self.assertIn("final_decision_status: current", status)
        self.assertIn("source_identity: " + controller.format_identity(identity), self.dry_run_output())

    # ---- C06-FR2 ------------------------------------------------------

    def test_fr2_source_change_during_verification_is_not_current(self):
        self.start()
        self.during_verification = lambda: (self.root / "written_during_checks.py").write_text("x = 1\n", encoding="utf-8")

        code = self.pipeline()

        self.assertEqual(code, EXIT_BLOCKED)
        state = self.state()
        self.assertEqual(state["state"], "awaiting_human_test")
        self.assertFalse((self.task_dir / CONTRACTS["06"].filename).exists())
        record = state["evidence"]["verification"][-1]
        self.assertEqual(record["status"], "passed")
        self.assertFalse(record["source_current"])
        self.assertIn("changed during verification", state["human_checkpoint"]["reason"])
        handoff = json.loads((self.task_dir / "05_supervisor_handoff.json").read_text(encoding="utf-8"))
        self.assertNotEqual(handoff["route"], "auto_verified")

    def test_fr2_manual_notes_must_cite_the_identity_status_exposes(self):
        self.start()
        self.verification_outcome = "failed"
        self.assertEqual(self.pipeline(), EXIT_BLOCKED)
        identity = self.identity()
        cited = controller.format_identity(identity)
        self.assertEqual(self.state()["human_checkpoint"]["source_identity"], cited)
        self.assertIn("source_identity: " + cited, self.status_output())
        self.assertIn(cited, self.state()["next_required_human_action"])

        cases = (
            ("no citation", "# Stage 6 - Manual test notes\n\n## Decision\n\n- [x] Accept\n- [ ] Reject\n- [ ] Needs follow-up\n", "do not cite"),
            ("wrong identity", self.manual_notes(identity={"fingerprint": "0" * 64}), "but the current source identity is"),
            ("two identities", self.manual_notes().replace(cited, cited + "\nsha256:" + "1" * 64), "more than one"),
        )
        for label, text, reason in cases:
            with self.subTest(label=label):
                self.write_notes(text)
                self.assertEqual(self.pipeline(), EXIT_BLOCKED)
                state = self.state()
                self.assertEqual(state["state"], "awaiting_human_test")
                self.assertIn(reason, state["human_checkpoint"]["reason"])
                self.assertFalse((self.task_dir / CONTRACTS["07"].filename).exists())
                self.assertEqual(self.dispatches("07"), 0)

        self.write_notes(self.manual_notes())
        self.assertEqual(self.pipeline(), EXIT_SUCCESS)
        state = self.state()
        self.assertEqual(state["evidence"]["stage06"][-1]["route"], "manual")
        self.assertEqual(state["evidence"]["stage06"][-1]["source"]["fingerprint"], identity["fingerprint"])
        self.assertEqual(self.dispatches("07"), 1)

    def test_fr2_source_change_during_review_is_not_bound(self):
        self.start()
        original = controller.ensure_real_stage

        def review_then_edit(*args, **kwargs):
            code = original(*args, **kwargs)
            if args[3] == "07":
                (self.root / "edited_during_review.py").write_text("y = 2\n", encoding="utf-8")
            return code

        controller.ensure_real_stage = review_then_edit
        self.addCleanup(setattr, controller, "ensure_real_stage", original)

        code = self.pipeline()

        self.assertEqual(code, EXIT_BLOCKED)
        state = self.state()
        self.assertIn("during Stage 7 review", state["last_failure"]["reason"])
        self.assertIn("unbound", [record["status"] for record in state["evidence"]["stage07"]])
        self.assertFalse((self.task_dir / CONTRACTS["07"].filename).exists())
        self.assertFalse((self.task_dir / CONTRACTS["08"].filename).exists())
        self.assertIn(CONTRACTS["07"].filename, self.history_files())

    # ---- C06-FR3 ------------------------------------------------------

    def test_fr3_source_changes_after_acceptance_require_new_evidence_not_implementation(self):
        (self.root / "app.py").write_text("print('v1')\n", encoding="utf-8")
        (self.root / "lib.py").write_text("X = 1\n", encoding="utf-8")
        self.git("add", "app.py", "lib.py", ".gitignore")
        self.git("commit", "-q", "-m", "base")
        self.start()

        def staged():
            (self.root / "lib.py").write_text("X = 2\n", encoding="utf-8")
            self.git("add", "lib.py")

        mutations = (
            ("dirty", lambda: (self.root / "app.py").write_text("print('v2')\n", encoding="utf-8")),
            ("staged", staged),
            ("committed", lambda: self.git("commit", "-q", "-am", "commit drift")),
            ("deleted", lambda: (self.root / "app.py").unlink()),
            ("renamed", lambda: self.git("mv", "lib.py", "lib_renamed.py")),
            ("untracked", lambda: (self.root / "new_module.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")),
        )
        for index, (label, mutate) in enumerate(mutations):
            with self.subTest(change=label):
                if index:
                    self.setUp_for_second_task("c06-drift-%s" % label)
                self.accept()
                old_decision = (self.task_dir / CONTRACTS["08"].filename).read_text(encoding="utf-8")
                verifications = len(self.verification_calls)
                stage5 = self.dispatches("05")
                reviews = self.dispatches("07")

                mutate()
                status = self.status_output()
                self.assertIn("final_decision: accept", status)
                self.assertIn("final_decision_status: historical", status)
                self.assertIn("current_acceptance: no", status)

                self.assertEqual(self.pipeline(), EXIT_SUCCESS)
                self.assertEqual(len(self.verification_calls), verifications + 1)
                self.assertEqual(self.dispatches("05"), stage5)
                self.assertEqual(self.dispatches("07"), reviews + 1)
                history = self.history_files()
                for stage in ("06", "07", "08"):
                    self.assertIn(CONTRACTS[stage].filename, history)
                archived = [p for p in (orchestrator_dir(self.task_dir) / "history").glob("*/" + CONTRACTS["08"].filename)]
                self.assertTrue(any(p.read_text(encoding="utf-8") == old_decision for p in archived))
                self.assertEqual(self.state()["evidence"]["stage08"][-1]["source"]["fingerprint"], self.identity()["fingerprint"])
                self.assertIn("final_decision_status: current", self.status_output())

    def test_fr3_audit_a3_new_failing_source_revokes_old_acceptance(self):
        # Inverts docs/audits reproduction A3: the old acceptance must not
        # survive a new source file, and verification must run again.
        self.start()
        self.accept()
        (self.root / "broken.py").write_text("raise RuntimeError('runtime failure')\n", encoding="utf-8")
        self.verification_outcome = "failed"
        stage5 = self.dispatches("05")

        code = self.pipeline()

        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(len(self.verification_calls), 2)
        self.assertEqual(self.dispatches("05"), stage5)
        state = self.state()
        self.assertEqual(state["state"], "awaiting_human_test")
        self.assertFalse((self.task_dir / CONTRACTS["08"].filename).exists())
        self.assertIn(CONTRACTS["08"].filename, self.history_files())
        self.assertEqual((self.root / "broken.py").read_text(encoding="utf-8"), "raise RuntimeError('runtime failure')\n")
        self.assertIn("current_acceptance: no", self.status_output())

    def test_fr3_unchanged_source_and_evidence_resume_without_repeating_work(self):
        self.start()
        self.accept()
        before = self.count_path.read_text(encoding="utf-8")

        self.assertEqual(self.pipeline(), EXIT_SUCCESS)

        self.assertEqual(len(self.verification_calls), 1)
        self.assertEqual(self.count_path.read_text(encoding="utf-8"), before)
        self.assertEqual(self.history_files(), [])

    def test_fr3_configuration_changes_invalidate_applicable_evidence(self):
        self.start()
        cfg = self.config()
        controller.load_config = lambda: cfg
        self.accept()

        cfg["turn_budgets"]["07"] = 6  # review configuration only
        self.assertEqual(self.pipeline(), EXIT_SUCCESS)
        self.assertEqual(len(self.verification_calls), 1)
        self.assertEqual(self.dispatches("07"), 2)

        cfg["verification"] = {"driven_project_commands": [{"name": "fixture", "argv": ["true"]}]}
        # R02-FR2/FR3: verification configuration is not part of the review
        # identity.  This fixture produces the same bound Stage 6 artifact,
        # so repeated invalidation cannot refill the already-used allowance.
        self.assertEqual(self.pipeline(), EXIT_BLOCKED)
        self.assertEqual(len(self.verification_calls), 2)
        self.assertEqual(self.dispatches("07"), 2)
        failure = self.state()["last_failure"]["reason"]
        self.assertIn("review-input identity", failure)
        # F03-FR5: no approval exists for an exhausted review identity, so
        # the reason must not point at approve-retry.
        self.assertNotIn("approve-retry", failure)
        self.assertIn("a new allowance requires a source, review-input, review-config, or bound Stage 6 change", failure)

    def test_fr3_manual_acceptance_drift_preserves_notes_and_requires_new_manual_evidence(self):
        self.start()
        self.verification_outcome = "failed"
        self.pipeline()
        self.write_notes(self.manual_notes())
        self.assertEqual(self.pipeline(), EXIT_SUCCESS)
        notes = (self.task_dir / CONTRACTS["06"].filename).read_text(encoding="utf-8")
        stage5 = self.dispatches("05")

        (self.root / "drift.py").write_text("z = 3\n", encoding="utf-8")
        code = self.pipeline()

        self.assertEqual(code, EXIT_BLOCKED)
        state = self.state()
        self.assertEqual(state["state"], "awaiting_human_test")
        self.assertIn("source identity changed", state["human_checkpoint"]["reason"])
        self.assertEqual((self.task_dir / CONTRACTS["06"].filename).read_text(encoding="utf-8"), notes)
        self.assertFalse((self.task_dir / CONTRACTS["07"].filename).exists())
        self.assertFalse((self.task_dir / CONTRACTS["08"].filename).exists())
        for stage in ("06", "07", "08"):
            self.assertIn(CONTRACTS[stage].filename, self.history_files())
        self.assertEqual(self.dispatches("05"), stage5)
        self.assertEqual(self.pipeline(), EXIT_BLOCKED)  # still stale until retested

        self.write_notes(self.manual_notes())
        self.assertEqual(self.pipeline(), EXIT_SUCCESS)
        self.assertEqual(self.dispatches("07"), 2)

    # ---- C06-FR4 ------------------------------------------------------

    def test_fr4_legacy_decision_without_identities_is_historical_not_current(self):
        self.start()
        self.accept()
        state = self.state()
        state.pop("evidence")
        legacy = "# Stage 6 - Manual test notes\n\n## Decision\n\n- [x] Accept\n- [ ] Reject\n- [ ] Needs follow-up\n"
        self.write_notes(legacy)
        state["input_hashes"][CONTRACTS["06"].filename] = sha256_file(self.task_dir / CONTRACTS["06"].filename)
        write_state_atomic(self.task_dir, state)

        status = self.status_output()
        self.assertIn("final_decision: accept", status)
        self.assertIn("final_decision_status: historical", status)
        self.assertIn("current_acceptance: no", status)
        out = io.StringIO()
        with redirect_stdout(out):
            controller.pipeline_report(self.task)
        self.assertIn("Final decision: **accept** (historical", out.getvalue())
        self.assertIn("Current acceptance: **no**", out.getvalue())
        report = json.loads((orchestrator_dir(self.task_dir) / "task_report.json").read_text(encoding="utf-8"))
        self.assertFalse(report["decision"]["current"])

        code = self.pipeline()

        self.assertNotEqual(code, EXIT_SUCCESS)
        self.assertEqual(self.state()["state"], "awaiting_human_test")
        self.assertIn("do not cite", self.state()["human_checkpoint"]["reason"])
        self.assertEqual((self.task_dir / CONTRACTS["06"].filename).read_text(encoding="utf-8"), legacy)

    # ---- C06-FR5 ------------------------------------------------------

    def test_fr5_unavailable_snapshot_is_unverified(self):
        self.start()
        self.accept()
        original = controller.capture_source_identity

        def unavailable(*args, **kwargs):
            raise SourceIdentityError("simulated unreadable source")

        controller.capture_source_identity = unavailable
        self.addCleanup(setattr, controller, "capture_source_identity", original)

        self.assertEqual(self.pipeline(), EXIT_BLOCKED)
        self.assertIn("simulated unreadable source", self.state()["last_failure"]["reason"])
        status = self.status_output()
        self.assertIn("source_identity: unavailable", status)
        self.assertIn("current_acceptance: no", status)

    def test_fr5_unavailable_snapshot_blocks_automatic_route(self):
        self.start()
        original = controller.capture_source_identity
        calls = []

        def fail_after_verification(*args, **kwargs):
            calls.append(1)
            if self.verification_calls:
                raise SourceIdentityError("simulated capture failure")
            return original(*args, **kwargs)

        controller.capture_source_identity = fail_after_verification
        self.addCleanup(setattr, controller, "capture_source_identity", original)

        self.assertEqual(self.pipeline(), EXIT_BLOCKED)
        state = self.state()
        # C07 strengthens this boundary: an unavailable read-only comparison
        # blocks the invocation rather than permitting a later checkpoint.
        self.assertEqual(state["state"], "blocked")
        self.assertIn("comparison unavailable", state["last_failure"]["reason"])
        self.assertFalse(state["evidence"]["verification"][-1]["source_current"])
        self.assertFalse((self.task_dir / CONTRACTS["06"].filename).exists())

    def test_fr5_internally_inconsistent_recorded_identity_is_not_current(self):
        self.start()
        self.accept()
        state = self.state()
        state["evidence"]["stage06"][-1]["source"]["worktree"] = "f" * 64
        write_state_atomic(self.task_dir, state)
        self.assertIn("current_acceptance: no", self.status_output())

        self.verification_outcome = "failed"
        self.assertEqual(self.pipeline(), EXIT_BLOCKED)
        self.assertEqual(len(self.verification_calls), 2)

    def test_fr5_later_failed_verification_supersedes_older_pass(self):
        self.start()
        self.accept()
        self.verification_outcome = "failed"
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(controller.pipeline_verify(self.task), EXIT_VALIDATION)
        self.assertIn("current_acceptance: no", self.status_output())

        code = self.pipeline()

        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(self.state()["state"], "awaiting_human_test")
        self.assertFalse((self.task_dir / CONTRACTS["08"].filename).exists())

    def test_fr5_interrupted_verification_supersedes_older_pass(self):
        self.start()
        self.accept()
        self.verification_outcome = "interrupt"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(controller.pipeline_verify(self.task), controller.EXIT_INTERRUPTED)
        self.assertEqual(self.state()["evidence"]["verification"][-1]["status"], "interrupted")
        self.assertIn("current_acceptance: no", self.status_output())

    def test_fr5_restoring_archived_pass_cannot_restore_acceptance(self):
        self.start()
        self.accept()
        drift = self.root / "drift.py"
        drift.write_text("a = 1\n", encoding="utf-8")
        self.verification_outcome = "failed"
        self.assertEqual(self.pipeline(), EXIT_BLOCKED)

        drift.unlink()  # source returns to the originally accepted identity
        history = orchestrator_dir(self.task_dir) / "history"
        for stage in ("06", "07", "08"):
            name = CONTRACTS[stage].filename
            copies = sorted(history.glob("*/" + name))
            shutil.copy2(str(copies[0]), str(self.task_dir / name))

        code = self.pipeline()

        self.assertNotEqual(code, EXIT_SUCCESS)
        self.assertIn("controller-generated automatic record", self.state()["human_checkpoint"]["reason"])
        self.assertIn("current_acceptance: no", self.status_output())

    def test_fr5_verification_status_uses_newest_attempt_for_same_inputs(self):
        self.start()
        self.accept()
        state = self.state()
        identity = self.identity()
        inputs = evidence.current_inputs(self.task_dir, self.config())
        older = state["evidence"]["verification"][-1]
        self.assertTrue(evidence.verification_status(state, identity, inputs, record=older)["current"])
        evidence.append(state, "verification", dict(older, status="failed"))
        self.assertFalse(evidence.verification_status(state, identity, inputs, record=older)["current"])
        self.assertFalse(evidence.stage06_status(self.task_dir, state, identity, inputs)["current"])


if __name__ == "__main__":
    unittest.main()
