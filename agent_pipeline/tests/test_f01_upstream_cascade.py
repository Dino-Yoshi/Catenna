"""F01 regressions: current acceptance respects the full upstream cascade.

Audit I1: `evidence.evaluate` compared only `00`/`01` with their acknowledged
hashes, so an edit to `02`/`03` after acceptance left `current_acceptance: yes`
while `current_stage` was `03` and `run` re-dispatched Stage 03. These tests
use the real-worktree C06 fixture with fake agent CLIs only.
"""

from __future__ import print_function

import io
import re
import unittest
from contextlib import redirect_stdout
from unittest import mock

from agent_pipeline import controller, evidence, state as state_module
from agent_pipeline.failures import EXIT_BLOCKED, EXIT_SUCCESS
from agent_pipeline.state import CONTRACTS, reconcile_artifacts
from agent_pipeline.tests import test_c06_evidence as c06
from agent_pipeline.tests import test_real_pipeline as base


UPSTREAM = ("00", "01", "02", "03", "04", "04_gate", "05")


class F01UpstreamCascadeTests(unittest.TestCase):
    setUp = base.RealPipelineTests.setUp
    tearDown = base.RealPipelineTests.tearDown
    config = base.RealPipelineTests.config
    write_fake_agent = base.RealPipelineTests.write_fake_agent
    verification_report = base.RealPipelineTests.verification_report
    manual_notes = base.RealPipelineTests.manual_notes
    start = c06.C06EvidenceTests.start
    git = c06.C06EvidenceTests.git
    dispatches = c06.C06EvidenceTests.dispatches
    state = c06.C06EvidenceTests.state
    identity = c06.C06EvidenceTests.identity
    status_output = c06.C06EvidenceTests.status_output
    dry_run_output = c06.C06EvidenceTests.dry_run_output
    pipeline = c06.C06EvidenceTests.pipeline
    accept = c06.C06EvidenceTests.accept

    def report_output(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(controller.pipeline_report(self.task), EXIT_SUCCESS)
        return out.getvalue()

    def path(self, stage):
        return self.task_dir / CONTRACTS[stage].filename

    def edit(self, stage):
        path = self.path(stage)
        original = path.read_text(encoding="utf-8")
        path.write_text(original + "\nOperator edit after acceptance (F01 probe).\n", encoding="utf-8")
        return original

    def field(self, output, name):
        match = re.search(r"^%s: (.*)$" % re.escape(name), output, re.MULTILINE)
        self.assertIsNotNone(match, "%s missing from output:\n%s" % (name, output))
        return match.group(1)

    def assert_not_current(self, stage, kind, redispatch):
        """F01-FR1/FR3/FR4 across status, dry-run, and report."""
        filename = CONTRACTS[stage].filename
        status = self.status_output()
        acceptance = self.field(status, "current_acceptance")
        self.assertTrue(acceptance.startswith("no"), status)
        self.assertIn("%s (%s)" % (filename, kind), acceptance)
        self.assertIn("re-dispatch Stage %s" % redispatch, acceptance)
        # F01-FR4: never `yes` while current_stage precedes 06.
        self.assertEqual(self.field(status, "current_stage"), redispatch)
        for label in ("stage06_evidence", "stage07_evidence", "stage08_evidence"):
            self.assertTrue(self.field(status, label).startswith("not current"), label)

        dry = self.dry_run_output()
        self.assertEqual(self.field(dry, "would_resume_at"), redispatch)
        self.assertTrue(self.field(dry, "current_acceptance").startswith("no"), dry)
        self.assertIn(filename, self.field(dry, "current_acceptance"))

        report = self.report_output()
        self.assertIn("Current acceptance: **no**", report)
        self.assertIn(filename, report)

    # ---- valid automatic-acceptance path --------------------------------

    def test_f01_unedited_accepted_task_stays_current_everywhere(self):
        self.start()
        self.accept()

        status = self.status_output()
        self.assertTrue(self.field(status, "current_acceptance").startswith("yes"), status)
        self.assertEqual(self.field(status, "current_stage"), "None")
        self.assertTrue(self.field(self.dry_run_output(), "current_acceptance").startswith("yes"))
        self.assertIn("Current acceptance: **yes**", self.report_output())
        stale = state_module.upstream_staleness(self.task_dir, self.state(), "06")
        self.assertFalse(stale["stale"])
        self.assertEqual(stale["artifacts"], [])

    # ---- F01-FR1 / FR3 / FR4 ---------------------------------------------

    def test_f01_fr1_fr3_fr4_editing_any_upstream_artifact_revokes_current_acceptance(self):
        self.start()
        self.accept()
        for index, stage in enumerate(UPSTREAM):
            with self.subTest(stage=stage):
                original = self.edit(stage)
                try:
                    # 00 and 01 are both consumed by 02, so either re-dispatches 02.
                    # A changed Stage 5 report re-dispatches Stage 6.
                    redispatch = "02" if stage in ("00", "01") else (UPSTREAM + ("06",))[index + 1]
                    self.assert_not_current(stage, "changed", redispatch)
                finally:
                    self.path(stage).write_text(original, encoding="utf-8")
                self.assertTrue(self.field(self.status_output(), "current_acceptance").startswith("yes"))

    def test_f01_fr1_missing_upstream_artifact_revokes_current_acceptance(self):
        self.start()
        self.accept()
        self.path("02").unlink()

        self.assert_not_current("02", "missing", "02")

    def test_f01_fr1_invalid_upstream_artifact_revokes_current_acceptance(self):
        self.start()
        self.accept()
        # Structurally invalid evidence: the required heading is gone.
        self.path("03").write_text("not an audit\n", encoding="utf-8")

        status = self.status_output()
        acceptance = self.field(status, "current_acceptance")
        self.assertTrue(acceptance.startswith("no"), status)
        self.assertIn(CONTRACTS["03"].filename, acceptance)
        self.assertIn("re-dispatch Stage 03", acceptance)
        self.assertEqual(self.field(status, "current_stage"), "03")

    def test_f01_fr1_unacknowledged_upstream_artifact_is_not_current(self):
        self.start()
        state = self.accept()
        state["input_hashes"].pop(CONTRACTS["03"].filename)

        summary = evidence.evaluate(self.task_dir, state, self.config(), self.identity())

        self.assertFalse(summary["current_acceptance"])
        self.assertIn("%s (unacknowledged)" % CONTRACTS["03"].filename, summary["reason"])

    # ---- acceptance: edit, run, back to yes --------------------------------

    def test_f01_edit_02_after_acceptance_redispatches_03_and_restores_acceptance(self):
        self.start()
        self.accept()
        before = {stage: self.dispatches(stage) for stage in ("02", "03")}
        self.edit("02")
        self.assert_not_current("02", "changed", "03")

        self.assertEqual(self.pipeline(), EXIT_SUCCESS)

        self.assertEqual(self.dispatches("02"), before["02"])
        self.assertEqual(self.dispatches("03"), before["03"] + 1)
        self.assertTrue(self.field(self.status_output(), "current_acceptance").startswith("yes"))

    def test_f01_edit_03_after_acceptance_redispatches_04_and_restores_acceptance(self):
        self.start()
        self.accept()
        before = {stage: self.dispatches(stage) for stage in ("03", "04")}
        self.edit("03")
        self.assert_not_current("03", "changed", "04")

        self.assertEqual(self.pipeline(), EXIT_SUCCESS)

        self.assertEqual(self.dispatches("03"), before["03"])
        self.assertEqual(self.dispatches("04"), before["04"] + 1)
        self.assertTrue(self.field(self.status_output(), "current_acceptance").startswith("yes"))

    # ---- F01-FR2 -------------------------------------------------------------

    def test_f01_fr2_staleness_uses_the_reconcile_rule(self):
        self.start()
        self.accept()
        for stage in UPSTREAM:
            with self.subTest(stage=stage):
                original = self.edit(stage)
                try:
                    snapshot = self.state()
                    reconciled = self.state()
                    reconcile_artifacts(self.task_dir, reconciled, read_only=True)
                    stale = state_module.upstream_staleness(self.task_dir, snapshot, "06")
                    self.assertTrue(stale["stale"])
                    self.assertEqual(stale["redispatch_stage"], reconciled["current_stage"])
                    self.assertEqual([name for name, _ in stale["artifacts"]], [CONTRACTS[stage].filename])
                    self.assertEqual(snapshot, self.state(), "staleness evaluation must be read-only")
                finally:
                    self.path(stage).write_text(original, encoding="utf-8")

    def test_f01_fr2_evaluate_and_reconcile_share_one_helper(self):
        self.start()
        state = self.accept()
        with mock.patch.object(state_module, "artifact_view", wraps=state_module.artifact_view) as view:
            reconcile_artifacts(self.task_dir, dict(state), read_only=True)
            self.assertEqual(view.call_count, 1)
            evidence.evaluate(self.task_dir, state, self.config(), self.identity())
            self.assertEqual(view.call_count, 2)

    # ---- F01-FR4: acceptance boundary ----------------------------------------

    def test_f01_fr4_acceptance_boundary_blocks_on_upstream_change(self):
        self.start()
        state = self.accept()
        self.edit("02")

        code, decision = controller.ensure_current_decision(self.task_dir, state, self.config())

        self.assertEqual(code, EXIT_BLOCKED)
        self.assertIsNone(decision)
        self.assertEqual(state["state"], "blocked")
        reason = (state.get("last_failure") or {}).get("reason") or ""
        self.assertIn(CONTRACTS["02"].filename, reason)
        self.assertIn("re-dispatch Stage 03", reason)


if __name__ == "__main__":
    unittest.main()
