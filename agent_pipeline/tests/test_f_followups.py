"""F05 regressions: the V2 cleanup follow-up gate and its evidence record.

F05-FR1: the 2026-09-25 consistency-audit probes (`probe_upstream.py`,
`probe_writer_budget.py`, `probe_edited_review.py`, plus the I3/I5 budget
observations) are converted here into permanent tests with inverted
expectations, one class per finding I1-I5. They replay the probes against the
same real-worktree fixture (`RemediationProbeFixture`) with fake agent CLIs
only; no paid provider is invoked.

F05-FR2 to FR5: the follow-up gate record, the amended SRS wording, the
operator documentation, and the refreshed status lines.
"""

from __future__ import print_function

import io
import json
import os
import re
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from agent_pipeline import attempts, controller
from agent_pipeline.failures import (
    EXIT_BAD_INPUT,
    EXIT_BLOCKED,
    EXIT_SUCCESS,
    FAILURE_CLASS_UNKNOWN_FAILURE,
)
from agent_pipeline.state import CONTRACTS
from agent_pipeline.tests import test_c09_cleanup_gate as cr_gate
from agent_pipeline.tests.test_r_cleanup_remediation import RemediationProbeFixture


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
V3PREP = REPOSITORY_ROOT / "docs" / "v3prep"
FOLLOWUP_GATE = V3PREP / "v2-cleanup-followups-gate.md"
FOLLOWUP_REQUIREMENTS = {
    "F%02d-FR%d" % (slice_number, requirement_number)
    for slice_number, count in {1: 4, 2: 5, 3: 5, 4: 3, 5: 5}.items()
    for requirement_number in range(1, count + 1)
}
FINDINGS = ["I%d" % number for number in range(1, 6)]
FINDING_SLICES = {"I1": "F01", "I2": "F03", "I3": "F03", "I4": "F04", "I5": "F02"}
THIS_MODULE = "agent_pipeline.tests.test_f_followups"
GATE_CLASS = THIS_MODULE + ".F05FollowupGateRecordTests."
TEST_ID_PATTERN = cr_gate.TEST_ID_PATTERN


def requirement_rows(text):
    rows = {}
    for line in text.splitlines():
        match = re.match(r"^\| (F\d{2}-FR\d+) \| (.*?) \| (PASS|OPEN) \|$", line)
        if match:
            rows[match.group(1)] = {"evidence": match.group(2), "status": match.group(3)}
    return rows


def finding_rows(text):
    rows = {}
    for line in text.splitlines():
        match = re.match(r"^\| (I\d+) \| (.*) \| (CLOSED|OPEN) \|$", line)
        if match:
            rows[match.group(1)] = {"evidence": match.group(2), "status": match.group(3)}
    return rows


def tested_commit(text):
    """The recorded tested commit, or None while only a working tree was tested."""
    match = re.search(r"(?m)^Tested revision: (.*)$", text)
    if not match or "working tree" in match.group(1):
        return None
    commit = re.fullmatch(r"`([0-9a-f]{40})`\.?", match.group(1).strip())
    return commit.group(1) if commit else None


def followup_gate_is_closed(text):
    """F05 closure rule: every F row PASS, I1-I5 CLOSED, suites PASS, and a
    committed tested revision. An uncommitted candidate keeps the gate open."""
    rows = requirement_rows(text)
    findings = finding_rows(text)
    return (
        set(rows) == FOLLOWUP_REQUIREMENTS
        and all(row["status"] == "PASS" for row in rows.values())
        and sorted(findings) == FINDINGS
        and all(row["status"] == "CLOSED" for row in findings.values())
        and "`python3 -m unittest discover -s agent_pipeline/tests` | PASS" in text
        and "`python3 -m agent_pipeline.cli mock-test` | PASS" in text
        and tested_commit(text) is not None
    )


def flat(text):
    """Collapse Markdown line wrapping so phrases can be matched."""
    return " ".join(text.split())


def recorded_status(text):
    match = re.search(r"(?m)^Status: \*\*(open|closed)\*\*", text)
    return match.group(1) if match else None


class FollowupProbeFixture(RemediationProbeFixture):
    def use_budget(self, budget):
        cfg = self.use_config(self.config())
        cfg["stage_attempt_budget"] = budget
        return cfg

    def approve(self, approval_id):
        out = io.StringIO()
        with redirect_stdout(out):
            code = controller.approve_retry(self.task, approval_id)
        return code, out.getvalue()

    def edit(self, stage, text="\nOperator added: new constraint.\n"):
        path = self.task_dir / CONTRACTS[stage].filename
        original = path.read_text(encoding="utf-8")
        path.write_text(original + text, encoding="utf-8")
        return original

    def field(self, output, name):
        match = re.search(r"^%s: (.*)$" % re.escape(name), output, re.MULTILINE)
        self.assertIsNotNone(match, "%s missing from output:\n%s" % (name, output))
        return match.group(1)

    def attempt_dirs(self, stage):
        root = attempts.attempts_root(self.task_dir)
        found = []
        for directory in sorted(root.iterdir()):
            path = directory / "dispatch.json"
            if path.is_file() and json.loads(path.read_text(encoding="utf-8")).get("stage") == stage:
                found.append(directory)
        return found


class I1UpstreamCascadeProbeTests(FollowupProbeFixture):
    """I1 (F01): probe_upstream.py saw `current_acceptance: yes` after an
    upstream edit to 02/03 while `run` re-dispatched. Inverted here."""

    def assert_edit_revokes_then_redispatches(self, stage, redispatch):
        self.start()
        self.accept()
        before = {key: self.dispatches(key) for key in ("02", "03", "04", "05", "07")}
        self.edit(stage)

        status = self.status_output()
        acceptance = self.field(status, "current_acceptance")
        self.assertTrue(acceptance.startswith("no"), status)
        self.assertIn(CONTRACTS[stage].filename, acceptance)
        self.assertIn("re-dispatch Stage %s" % redispatch, acceptance)
        self.assertEqual(self.field(status, "current_stage"), redispatch)

        self.assertEqual(self.pipeline(), EXIT_SUCCESS)

        after = {key: self.dispatches(key) for key in before}
        self.assertEqual(after[redispatch], before[redispatch] + 1)
        for key in before:
            if key < redispatch:
                self.assertEqual(after[key], before[key], key)
        self.assertTrue(self.field(self.status_output(), "current_acceptance").startswith("yes"))

    def test_i1_edit_02_after_acceptance_is_not_current_and_redispatches_03(self):
        self.assert_edit_revokes_then_redispatches("02", "03")

    def test_i1_edit_03_after_acceptance_is_not_current_and_redispatches_04(self):
        self.assert_edit_revokes_then_redispatches("03", "04")

    def test_i1_edit_00_after_acceptance_still_redispatches_02(self):
        # The probe's control case (already fixed by R01) stays fixed.
        self.assert_edit_revokes_then_redispatches("00", "02")


class I2FailedWriterExhaustionProbeTests(FollowupProbeFixture):
    """I2 (F03): probe_writer_budget.py saw budget 1 create an approval that,
    once approved, was consumed and blocked without dispatching. Inverted."""

    def run_failing_writer(self, budget):
        self.start()
        self.use_budget(budget)
        original = controller.invoke_agent
        writes = []

        def failing_writer(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[3] == "05":
                writes.append(True)
                if len(writes) == 1:
                    (self.root / "partial.py").write_text("x = 1\n", encoding="utf-8")
                    result.update(exit_code=9, status="failed", failure_class=FAILURE_CLASS_UNKNOWN_FAILURE)
            return result

        patcher = mock.patch.object(controller, "invoke_agent", side_effect=failing_writer)
        patcher.start()
        self.addCleanup(patcher.stop)
        return writes

    def test_i2_budget1_failed_writer_creates_no_approval_and_names_path(self):
        writes = self.run_failing_writer(1)

        self.assertEqual(self.pipeline(), EXIT_BLOCKED)

        state = self.state()
        self.assertEqual(len(writes), 1)
        self.assertEqual(state["state"], "blocked")
        self.assertIsNone(state.get("pending_approval"))
        self.assertFalse(state.get("approval_history"))
        reason = self.reason()
        self.assertIn("partial.py", reason)
        self.assertIn("further writers are blocked", reason)
        self.assertIn("no retry approval was created", reason)
        self.assertIn("a new allowance requires a change to its consumed inputs", reason)
        self.assertNotIn("approve-retry", reason)
        # The probe's approve step now has nothing to approve or consume.
        code, output = self.approve("retry-anything")
        self.assertEqual(code, EXIT_BAD_INPUT)
        self.assertIn("no pending approval", output)
        self.assertEqual(self.pipeline(), EXIT_BLOCKED)
        self.assertEqual(len(writes), 1)
        # User files are never reset or reverted.
        self.assertEqual((self.root / "partial.py").read_text(encoding="utf-8"), "x = 1\n")

    def test_i2_budget2_failed_writer_still_requires_approval_then_second_writer(self):
        writes = self.run_failing_writer(2)

        self.assertEqual(self.pipeline(), EXIT_BLOCKED)
        state = self.state()
        self.assertEqual(state["state"], "awaiting_retry_approval")
        pending = state["pending_approval"]
        self.assertIn("partial.py", [item["path"] for item in pending["failed_source_changes"]])
        self.assertEqual(len(writes), 1)

        code, _ = self.approve(pending["approval_id"])
        self.assertEqual(code, EXIT_SUCCESS)
        self.pipeline()

        state = self.state()
        self.assertEqual(len(writes), 2)
        self.assertIn("05", state["completed_stages"])
        self.assertIsNone(state.get("pending_approval"))
        # The approval is consumed by the step that dispatched the second writer.
        dispatches = sorted(
            (json.loads((path / "dispatch.json").read_text(encoding="utf-8")) for path in self.attempt_dirs("05")),
            key=lambda record: record["attempt_number"],
        )
        self.assertEqual(dispatches[-1]["approval"]["approval_id"], pending["approval_id"])
        self.assertTrue(dispatches[-1]["approval"]["consumed"])


class I3ReviewExhaustionProbeTests(FollowupProbeFixture):
    """I3 (F03): an exhausted Stage 7 identity said "use approve-retry" while
    no pending approval existed. Inverted: the reason is actionable."""

    def test_i3_exhausted_review_identity_never_suggests_approve_retry(self):
        self.start()
        cfg = self.use_budget(1)
        self.accept()
        reviews = self.dispatches("07")
        cfg["turn_budgets"]["07"] += 1

        with mock.patch.dict(os.environ, {"FAKE_FAIL_STAGE": "07"}):
            self.assertEqual(self.pipeline(), EXIT_BLOCKED)
            self.assertEqual(self.pipeline(), EXIT_BLOCKED)
        self.assertEqual(self.dispatches("07"), reviews + 1)

        reason = self.reason()
        self.assertIn("budget exhausted for review-input identity", reason)
        self.assertIn(
            "a new allowance requires a source, review-input, review-config, or bound Stage 6 change",
            reason,
        )
        self.assertNotIn("approve-retry", reason)
        self.assertIsNone(self.state().get("pending_approval"))
        code, output = self.approve("retry-anything")
        self.assertEqual(code, EXIT_BAD_INPUT)
        self.assertIn("no pending approval", output)
        self.assertIn("review_attempts: 1/1", self.status_output())

        # `approve-retry` and USAGE.md agree: the exhausted block has no approval.
        usage = (REPOSITORY_ROOT / "docs/USAGE.md").read_text(encoding="utf-8")
        section = flat(usage[usage.index("### Re-review allowance"):usage.index("### Interruption exit behavior")])
        self.assertIn("creates no approval", section)
        self.assertNotIn("does not currently create one", section)


class I4HandEditedReviewProbeTests(FollowupProbeFixture):
    """I4 (F04): probe_edited_review.py saw a hand-edited review left unmarked
    by invalidation and then re-promoted over the edit. Inverted."""

    def accept_then_hand_edit(self, budget):
        self.start()
        self.use_budget(budget)
        self.accept()
        review = self.task_dir / CONTRACTS["07"].filename
        original = review.read_text(encoding="utf-8")
        review.write_text(original + "\nHand note.\n", encoding="utf-8")
        return review, original

    def test_i4_budget1_hand_edited_review_is_retired_and_not_restored(self):
        review, original = self.accept_then_hand_edit(1)
        before = self.dispatches("07")

        self.assertEqual(self.pipeline(), EXIT_BLOCKED)

        self.assertEqual(self.dispatches("07"), before)
        self.assertFalse(review.exists() and review.read_text(encoding="utf-8") == original)
        promoted = [path for path in self.attempt_dirs("07") if (path / "promoted.json").exists()]
        self.assertTrue(promoted)
        for directory in promoted:
            record = json.loads((directory / "invalidated.json").read_text(encoding="utf-8"))
            self.assertIs(record["hash_mismatch"], True)
        reason = self.reason()
        self.assertIn("Stage 7 attempt budget exhausted for review-input identity", reason)
        self.assertNotIn("approve-retry", reason)
        self.assertTrue(self.field(self.status_output(), "current_acceptance").startswith("no"))

        # The probe's second run does not resurrect the original either.
        self.assertEqual(self.pipeline(), EXIT_BLOCKED)
        self.assertEqual(self.dispatches("07"), before)
        self.assertFalse(review.exists() and review.read_text(encoding="utf-8") == original)

    def test_i4_budget2_hand_edited_review_gets_a_new_review(self):
        review, original = self.accept_then_hand_edit(2)
        before = self.dispatches("07")

        self.assertEqual(self.pipeline(), EXIT_SUCCESS)

        self.assertEqual(self.dispatches("07"), before + 1)
        self.assertNotIn("Hand note.", review.read_text(encoding="utf-8"))
        self.assertTrue(self.field(self.status_output(), "current_acceptance").startswith("yes"))


class I5StageInputBudgetProbeTests(FollowupProbeFixture):
    """I5 (F02): the R01 fresh allowance lived only in state.json and was lost
    after one failed current-input attempt, so a resume fell back to the
    lifetime counter. Inverted: the budget is per durable input identity."""

    def test_i5_budget2_allowance_survives_a_failed_attempt_and_resume(self):
        self.start()
        self.use_budget(2)
        self.accept()
        audits = self.dispatches("03")
        self.edit("02")

        with mock.patch.dict(os.environ, {"FAKE_FAIL_STAGE": "03"}):
            self.assertEqual(self.pipeline(), EXIT_BLOCKED)
            self.assertEqual(self.dispatches("03"), audits + 1)
            # Against 21d83c2 this resume granted nothing (lifetime count 2/2).
            self.assertEqual(self.pipeline(), EXIT_BLOCKED)
            self.assertEqual(self.dispatches("03"), audits + 2)
            # And never a third attempt for the same identity.
            self.assertEqual(self.pipeline(), EXIT_BLOCKED)
            self.assertEqual(self.dispatches("03"), audits + 2)

        state = self.state()
        self.assertNotIn("_r01_fresh_input_stages", state)
        identity = attempts.current_stage_input_identity(self.task_dir, "03")
        self.assertEqual(attempts.count_stage_input_attempts(self.task_dir, "03", identity), 2)
        self.assertIn("stage input identity " + identity, self.reason())
        self.assertIn("stage_attempts: 03 2/2 (stage input identity %s)" % identity, self.status_output())

    def test_i5_budget1_resume_grants_nothing_until_inputs_change(self):
        self.start()
        self.use_budget(1)
        self.accept()
        audits = self.dispatches("03")

        with mock.patch.dict(os.environ, {"FAKE_FAIL_STAGE": "03"}):
            self.edit("02", "\nFirst edit.\n")
            for _ in range(3):
                self.assertEqual(self.pipeline(), EXIT_BLOCKED)
            self.assertEqual(self.dispatches("03"), audits + 1)

            self.edit("02", "\nSecond edit.\n")
            for _ in range(2):
                self.assertEqual(self.pipeline(), EXIT_BLOCKED)
            self.assertEqual(self.dispatches("03"), audits + 2)
        self.assertNotIn("_r01_fresh_input_stages", self.state())


class F05AutomaticAcceptanceGuardTests(FollowupProbeFixture):
    """A1 stays closed across F01-F04: invalid evidence is ineligible and the
    valid controller-owned route still reaches automatic acceptance."""

    def test_f05_a1_invalid_evidence_stays_ineligible_and_valid_path_accepts(self):
        handoff = {"route": "manual_test"}
        passed = self.verification_report(overall_status="passed", coverage_status="ok", driven_project_verified=True)
        invalid = (
            (dict(self.config(), enable_auto_verified=False), passed),
            (self.config(), None),
            (self.config(), self.verification_report(overall_status="failed", coverage_status="ok", driven_project_verified=False)),
            (self.config(), self.verification_report(overall_status="passed", coverage_status="flagged", driven_project_verified=True)),
            (self.config(), self.verification_report()),
        )
        for config, report in invalid:
            with self.subTest(report=report):
                eligibility = controller.automatic_verification_eligibility(config, report, handoff)
                self.assertFalse(eligibility["eligible"])
                self.assertTrue(eligibility["reason"])
        # An agent-claimed `auto_verified` route is not controller evidence.
        claimed = controller.automatic_verification_eligibility(
            dict(self.config(), enable_auto_verified=False), passed, {"route": "auto_verified"}
        )
        self.assertFalse(claimed["eligible"])

        self.start()
        self.use_budget(1)
        state = self.accept()
        self.assertEqual(state["state"], "complete")
        self.assertTrue(state["evidence"]["verification"][-1]["source_current"])
        self.assertIn("- [x] Accept", (self.task_dir / CONTRACTS["06"].filename).read_text(encoding="utf-8"))
        self.assertIn("- [x] Accept", (self.task_dir / CONTRACTS["08"].filename).read_text(encoding="utf-8"))
        self.assertIsNone(state.get("pending_approval"))
        self.assertEqual(list(attempts.attempts_root(self.task_dir).glob("*/invalidated.json")), [])
        self.assertNotIn("stage_attempts:", self.status_output())


class F05FollowupGateRecordTests(unittest.TestCase):
    def setUp(self):
        self.text = FOLLOWUP_GATE.read_text(encoding="utf-8")

    def cited(self, rows):
        ids = []
        for row in rows.values():
            for test_id in TEST_ID_PATTERN.findall(row["evidence"]):
                if test_id not in ids:
                    ids.append(test_id)
        return ids

    def assert_loads(self, test_id):
        suite = unittest.defaultTestLoader.loadTestsFromName(test_id)
        self.assertEqual(suite.countTestCases(), 1, test_id)
        self.assertNotIn("_FailedTest", repr(suite), test_id)
        return suite

    def test_f05_fr1_each_finding_has_a_converted_probe_regression(self):
        rows = finding_rows(self.text)
        self.assertEqual(sorted(rows), FINDINGS)
        for finding, row in sorted(rows.items()):
            with self.subTest(finding=finding):
                self.assertEqual(row["status"], "CLOSED")
                # Columns after the ID: severity | observed | owning slice | regressions.
                self.assertEqual(row["evidence"].split(" | ")[2], FINDING_SLICES[finding])
                converted = [
                    test_id for test_id in TEST_ID_PATTERN.findall(row["evidence"])
                    if test_id.startswith(THIS_MODULE + ".")
                ]
                self.assertTrue(converted, "%s has no converted probe" % finding)
                for test_id in converted:
                    self.assertIn(".test_%s_" % finding.lower(), test_id)
                    self.assert_loads(test_id)
        # The probes run against real temporary Git worktrees with fake agents.
        self.assertTrue(issubclass(FollowupProbeFixture, RemediationProbeFixture))
        self.assertEqual(FollowupProbeFixture.start, RemediationProbeFixture.start)

    def test_f05_fr2_record_names_revision_date_environment_and_integration_results(self):
        self.assertRegex(self.text, r"(?m)^Tested revision: ")
        self.assertRegex(self.text, r"Tested\s+on\s+\d{4}-\d{2}-\d{2}")
        self.assertRegex(self.text, r"Linux [0-9.]+ and Python [0-9.]+")
        self.assertIn("No paid provider was invoked.", self.text)
        self.assertRegex(self.text, r"`python3 -m unittest discover -s agent_pipeline/tests` \| PASS \| \d+ tests")
        self.assertRegex(self.text, r"`python3 -m agent_pipeline.cli mock-test` \| PASS \| \d+ ")
        self.assertIn("optional non-editable packaging smoke", self.text)

    def test_f05_fr2_every_f_requirement_row_is_pass_with_loadable_evidence(self):
        rows = requirement_rows(self.text)
        self.assertEqual(set(rows), FOLLOWUP_REQUIREMENTS)
        for requirement, row in sorted(rows.items()):
            with self.subTest(requirement=requirement):
                self.assertEqual(row["status"], "PASS")
                test_ids = TEST_ID_PATTERN.findall(row["evidence"])
                self.assertTrue(test_ids, "%s has no test evidence" % requirement)
                for test_id in test_ids:
                    self.assert_loads(test_id)

    def test_f05_fr2_every_cited_test_passes(self):
        # Run every cited regression (not only load it). Gate-record tests are
        # excluded here to avoid recursion; the discovery run covers them.
        test_ids = [
            test_id for test_id in self.cited(requirement_rows(self.text)) + self.cited(finding_rows(self.text))
            if not test_id.startswith(GATE_CLASS)
        ]
        test_ids = list(dict.fromkeys(test_ids))
        self.assertGreater(len(test_ids), len(FOLLOWUP_REQUIREMENTS))
        suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(test_id) for test_id in test_ids)
        stream = io.StringIO()
        with redirect_stdout(io.StringIO()):
            result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
        self.assertEqual(result.testsRun, len(test_ids))
        self.assertEqual(result.skipped, [], stream.getvalue())
        self.assertTrue(result.wasSuccessful(), stream.getvalue())

    def test_f05_fr2_status_matches_the_closure_rule(self):
        status = recorded_status(self.text)
        self.assertIn(status, ("open", "closed"))
        # The record may say closed only when the closure rule holds; an
        # uncommitted candidate must be recorded as open.
        self.assertEqual(status == "closed", followup_gate_is_closed(self.text))
        if tested_commit(self.text) is None:
            self.assertEqual(status, "open")

        # The rule itself: a committed revision with every row PASS closes it,
        # and any single gap reopens it.
        committed = re.sub(r"(?m)^Tested revision: .*$", "Tested revision: `%s`." % ("a" * 40), self.text, count=1)
        self.assertTrue(followup_gate_is_closed(committed))
        working_tree = re.sub(
            r"(?m)^Tested revision: .*$", "Tested revision: `%s` plus the working tree." % ("a" * 40), self.text, count=1
        )
        self.assertFalse(followup_gate_is_closed(working_tree))
        for requirement in ("F01-FR1", "F03-FR3", "F05-FR2"):
            with self.subTest(requirement=requirement):
                row = next(line for line in committed.splitlines() if line.startswith("| %s |" % requirement))
                self.assertFalse(followup_gate_is_closed(committed.replace(row, row[:-len("PASS |")] + "OPEN |", 1)))
                self.assertFalse(followup_gate_is_closed(committed.replace(row + "\n", "", 1)))
        for finding in FINDINGS:
            with self.subTest(finding=finding):
                row = next(line for line in committed.splitlines() if line.startswith("| %s |" % finding))
                self.assertFalse(followup_gate_is_closed(committed.replace(row, row[:-len("CLOSED |")] + "OPEN |", 1)))

    def test_f05_fr2_cr_gate_record_stays_closed(self):
        cr_text = cr_gate.GATE_RECORD.read_text(encoding="utf-8")
        self.assertTrue(cr_gate.gate_is_closed(cr_text))
        self.assertRegex(cr_text, r"(?m)^Status: \*\*closed\*\*")
        # The follow-up gate is separate; it does not reopen the C/R record.
        self.assertFalse(set(requirement_rows(cr_text)))

    def test_f05_fr3_srs_wording_reflects_identity_budgets_and_no_dead_end_approval(self):
        srs = (V3PREP / "v2-cleanup.md").read_text(encoding="utf-8")
        c03 = next(line for line in srs.splitlines() if line.startswith("- C03-FR4:"))
        self.assertIn("consumed-input identity", c03)
        self.assertIn("shall not create or consume a retry approval", c03)
        self.assertIn("never refills", c03)
        c06 = next(line for line in srs.splitlines() if line.startswith("- C06-FR3:"))
        self.assertIn("review-input identity", c06)
        self.assertIn("consumed-input identity", c06)
        self.assertIn("without creating a retry approval", c06)
        self.assertNotIn("approve-retry", c06)

        cr_text = cr_gate.GATE_RECORD.read_text(encoding="utf-8")
        deferred = flat(cr_text[cr_text.index("Deferred follow-up"):])
        self.assertIn("Resolved by F03", deferred)
        self.assertIn("(v2-cleanup-followups.md#slice-f03--no-approval-when-no-attempt-can-follow)", deferred)

    def test_f05_fr4_usage_documents_identity_budgets_and_exhaustion_without_approval(self):
        usage = (REPOSITORY_ROOT / "docs/USAGE.md").read_text(encoding="utf-8")
        section = usage[usage.index("## Cleanup safety guarantees"):usage.index("## Config Reference")]
        self.assertIn("### Stage attempt allowance", section)
        allowance = flat(section[section.index("### Stage attempt allowance"):section.index("### Re-review allowance")])
        for text in (
            "consumed-input identity",
            "`stage_attempt_budget`",
            "stage_attempts",
            "never refills",
            "further writers are blocked",
            "no retry approval",
            "withdrawn",
            "approve-retry",
        ):
            self.assertIn(text, allowance)
        rereview = flat(section[section.index("### Re-review allowance"):section.index("### Interruption exit behavior")])
        self.assertIn("creates no approval", rereview)
        config_row = next(line for line in usage.splitlines() if line.startswith("| `stage_attempt_budget` |"))
        self.assertIn("per consumed-input identity", config_row)
        state_row = next(line for line in usage.splitlines() if line.startswith("| `awaiting_retry_approval` |"))
        self.assertIn("attempt remains", state_row)

    def test_f05_fr5_status_lines_entry_gate_and_i11_input(self):
        remediation = (V3PREP / "v2-cleanup-remediation.md").read_text(encoding="utf-8")
        self.assertNotIn("Status: scoped, not implemented.", remediation)
        remediation_status = next(line for line in remediation.splitlines() if line.startswith("Status:"))
        self.assertTrue(remediation_status.startswith("Status: implemented;"), remediation_status)
        self.assertIn("(v2-cleanup-gate.md) closed 2026-09-25", remediation_status)

        srs = (V3PREP / "v2-cleanup.md").read_text(encoding="utf-8")
        status_line = next(line for line in srs.splitlines() if line.startswith("Status:"))
        self.assertNotIn("reopened", status_line)
        self.assertIn("v2-cleanup-followups-gate.md", status_line)

        review = (V3PREP / "srs-review.md").read_text(encoding="utf-8")
        cleanup_row = next(line for line in review.splitlines() if line.startswith("| V2 cleanup |"))
        self.assertNotIn("gate reopened", cleanup_row)
        self.assertIn("gate closed 2026-09-25", cleanup_row)
        self.assertIn("F01–F05", cleanup_row)

        followups = (V3PREP / "v2-cleanup-followups.md").read_text(encoding="utf-8")
        self.assertNotIn("Status: scoped, not implemented.", followups)

        v3 = (V3PREP / "v3.md").read_text(encoding="utf-8")
        entry = next(line for line in v3.splitlines() if line.startswith("Status:"))
        self.assertIn("v2-cleanup.md#slice-c09--cleanup-completion-gate", entry)
        self.assertIn("v2-cleanup-followups-gate.md", entry)
        self.assertIn("both", entry)
        v01 = v3[v3.index("### Slice V01"):v3.index("### Slice V02")]
        self.assertIn("I11", v01)
        self.assertIn("state.json", v01)


if __name__ == "__main__":
    unittest.main()
