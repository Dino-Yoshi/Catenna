"""C09 regressions: the cleanup completion gate and its evidence record."""

from __future__ import print_function

import re
import unittest
from pathlib import Path

from agent_pipeline import controller
from agent_pipeline.failures import EXIT_SUCCESS
from agent_pipeline.state import CONTRACTS, load_state
from agent_pipeline.tests import test_real_pipeline as base


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
GATE_RECORD = REPOSITORY_ROOT / "docs" / "v3prep" / "v2-cleanup-gate.md"
CLEANUP_REQUIREMENTS = {
    "C%02d-FR%d" % (slice_number, requirement_number)
    for slice_number, count in {
        1: 3, 2: 3, 3: 4, 4: 3, 5: 3,
        6: 5, 7: 4, 8: 5, 9: 4,
    }.items()
    for requirement_number in range(1, count + 1)
}
# R08-FR2: the remediation slices R01-R08 are part of the same gate.
REMEDIATION_REQUIREMENTS = {
    "R%02d-FR%d" % (slice_number, requirement_number)
    for slice_number, count in {
        1: 5, 2: 4, 3: 4, 4: 4, 5: 5, 6: 3, 7: 3, 8: 4,
    }.items()
    for requirement_number in range(1, count + 1)
}
REQUIREMENTS = CLEANUP_REQUIREMENTS | REMEDIATION_REQUIREMENTS
DEFECTS = ["D%d" % number for number in range(1, 9)]
REMEDIATION_MODULE = REPOSITORY_ROOT / "agent_pipeline/tests/test_r_cleanup_remediation.py"
TEST_ID_PATTERN = re.compile(
    r"agent_pipeline\.tests\.test_[a-z0-9_]+\.[A-Za-z0-9_]+\.test_[a-z0-9_]+"
)


def requirement_rows(text):
    rows = {}
    for line in text.splitlines():
        match = re.match(r"^\| ([CR]\d{2}-FR\d+) \| (.*?) \| (PASS|OPEN) \|$", line)
        if match:
            rows[match.group(1)] = {"evidence": match.group(2), "status": match.group(3)}
    return rows


def defect_rows(text):
    rows = {}
    for line in text.splitlines():
        match = re.match(r"^\| (D\d) \| (.*) \| (CLOSED|OPEN) \|$", line)
        if match:
            rows[match.group(1)] = {"evidence": match.group(2), "status": match.group(3)}
    return rows


def gate_is_closed(text):
    rows = requirement_rows(text)
    return set(rows) == REQUIREMENTS and all(row["status"] == "PASS" for row in rows.values())


class C09CleanupGateRecordTests(unittest.TestCase):
    def setUp(self):
        self.text = GATE_RECORD.read_text(encoding="utf-8")

    def test_c09_fr1_each_finding_has_a_permanent_guard_using_required_fixtures(self):
        for finding in range(1, 9):
            self.assertRegex(self.text, r"(?m)^\| A%s \| C\d{2} \| .*test_" % finding)
        self.assertNotIn("reproduce-2026-09-21.py` | PASS", self.text)

        locking = (REPOSITORY_ROOT / "agent_pipeline/tests/test_locking.py").read_text(encoding="utf-8")
        runner = (REPOSITORY_ROOT / "agent_pipeline/tests/test_real_runner.py").read_text(encoding="utf-8")
        source = (REPOSITORY_ROOT / "agent_pipeline/tests/test_source_identity.py").read_text(encoding="utf-8")
        self.assertIn("subprocess.Popen", locking)
        self.assertIn("subprocess.Popen", runner)
        self.assertIn('git(self.root, "init"', source)

    def test_c09_fr2_required_commands_are_recorded_passing(self):
        self.assertIn("`python3 -m unittest discover -s agent_pipeline/tests` | PASS", self.text)
        self.assertIn("`python3 -m agent_pipeline.cli mock-test` | PASS", self.text)
        self.assertIn("The one skipped test is the optional non-editable packaging smoke", self.text)

    def test_c09_fr3_operator_documentation_covers_corrected_guarantees(self):
        usage = (REPOSITORY_ROOT / "docs/USAGE.md").read_text(encoding="utf-8")
        for heading in (
            "### Explicit decisions",
            "### Evidence freshness",
            "### Exclusive execution",
            "### Interruption and recovery",
            "### Legacy-task limitations",
        ):
            self.assertIn(heading, usage)
        self.assertIn("exactly one", usage)
        self.assertIn("historical", usage)
        self.assertIn("canonical Git worktree", usage)
        self.assertIn("five seconds", usage)

    def test_c09_fr4_every_requirement_maps_to_loadable_passing_evidence(self):
        rows = requirement_rows(self.text)
        self.assertEqual(set(rows), REQUIREMENTS)
        self.assertTrue(gate_is_closed(self.text))
        for requirement, row in rows.items():
            test_ids = TEST_ID_PATTERN.findall(row["evidence"])
            self.assertTrue(test_ids, "%s has no test evidence" % requirement)
            for test_id in test_ids:
                suite = unittest.defaultTestLoader.loadTestsFromName(test_id)
                self.assertGreater(suite.countTestCases(), 0, test_id)
                self.assertNotIn("_FailedTest", repr(suite), test_id)

        row = next(line for line in self.text.splitlines() if line.startswith("| C08-FR5 |"))
        opened = self.text.replace(row, row[:-len("PASS |")] + "OPEN |", 1)
        self.assertFalse(gate_is_closed(opened))

    def test_r08_fr2_gate_requires_every_c_and_r_row_to_pass(self):
        rows = requirement_rows(self.text)
        self.assertEqual(set(rows) & REMEDIATION_REQUIREMENTS, REMEDIATION_REQUIREMENTS)
        self.assertEqual(set(rows) & CLEANUP_REQUIREMENTS, CLEANUP_REQUIREMENTS)
        self.assertTrue(gate_is_closed(self.text))
        for requirement in ("C01-FR1", "C09-FR4", "R01-FR1", "R05-FR2", "R08-FR4"):
            with self.subTest(requirement=requirement):
                row = next(line for line in self.text.splitlines() if line.startswith("| %s |" % requirement))
                opened = self.text.replace(row, row[:-len("PASS |")] + "OPEN |", 1)
                self.assertFalse(gate_is_closed(opened))
                self.assertFalse(gate_is_closed(self.text.replace(row + "\n", "", 1)))

    def test_r08_fr1_fr3_every_defect_is_closed_by_a_passing_remediation_regression(self):
        self.assertRegex(self.text, r"(?m)^Status: \*\*closed\*\*")
        rows = defect_rows(self.text)
        self.assertEqual(sorted(rows), DEFECTS)
        module = REMEDIATION_MODULE.read_text(encoding="utf-8")
        for defect, row in sorted(rows.items()):
            with self.subTest(defect=defect):
                self.assertEqual(row["status"], "CLOSED")
                test_ids = [
                    test_id for test_id in TEST_ID_PATTERN.findall(row["evidence"])
                    if test_id.startswith("agent_pipeline.tests.test_r_cleanup_remediation.")
                ]
                self.assertTrue(test_ids, "%s has no remediation regression" % defect)
                for test_id in test_ids:
                    self.assertIn("test_%s_" % defect.lower(), test_id)
                    suite = unittest.defaultTestLoader.loadTestsFromName(test_id)
                    self.assertGreater(suite.countTestCases(), 0, test_id)
                    self.assertNotIn("_FailedTest", repr(suite), test_id)
        # R08-FR1: the converted probes use real worktrees and subprocesses.
        self.assertIn("subprocess.Popen", module)
        self.assertIn("test_c06_evidence", module)
        self.assertIn("ExecutionOwnership", module)

    def test_r08_fr3_record_names_revision_date_and_integration_results(self):
        self.assertRegex(self.text, r"Tested revision: `[0-9a-f]{40}`")
        self.assertRegex(self.text, r"Tested\s+on\s+\d{4}-\d{2}-\d{2}")
        self.assertRegex(self.text, r"`python3 -m unittest discover -s agent_pipeline/tests` \| PASS \| \d+ tests")
        self.assertNotIn("fails by design", self.text)

    def test_r08_fr4_operator_documentation_covers_remediated_behavior(self):
        usage = (REPOSITORY_ROOT / "docs/USAGE.md").read_text(encoding="utf-8")
        section = usage[usage.index("## Cleanup safety guarantees"):usage.index("## Config Reference")]
        for heading in (
            "### Manual acceptance with configured checks",
            "### Re-review allowance",
            "### Interruption exit behavior",
            "### Ignoring check output",
        ):
            self.assertIn(heading, section)
        self.assertIn("catenna verify", section)
        self.assertIn("review_input_identity", section)
        self.assertIn("review_attempts", section)
        self.assertIn("approve-retry", section)
        self.assertIn("130", section)
        self.assertIn("not_attempted", section)
        self.assertIn("__pycache__/", section)
        self.assertIn(".gitignore", section)


class C09AutomaticAcceptanceRegressionTests(unittest.TestCase):
    setUp = base.RealPipelineTests.setUp
    tearDown = base.RealPipelineTests.tearDown
    config = base.RealPipelineTests.config
    write_fake_agent = base.RealPipelineTests.write_fake_agent
    verification_report = base.RealPipelineTests.verification_report

    def test_c09_fr1_invalid_evidence_cannot_authorize_automatic_acceptance(self):
        handoff = {"route": "manual_test"}
        valid = self.verification_report(
            overall_status="passed", coverage_status="ok", driven_project_verified=True
        )
        cases = [
            (dict(self.config(), enable_auto_verified=False), valid),
            (self.config(), None),
            (self.config(), self.verification_report(overall_status="failed", coverage_status="ok", driven_project_verified=True)),
            (self.config(), self.verification_report(overall_status="passed", coverage_status="flagged", driven_project_verified=True)),
            (self.config(), self.verification_report(overall_status="passed", coverage_status="ok")),
        ]
        for config, report in cases:
            with self.subTest(report=report):
                eligibility = controller.automatic_verification_eligibility(config, report, handoff)
                self.assertFalse(eligibility["eligible"])
                self.assertTrue(eligibility["reason"])

    def test_c09_fr1_valid_controller_evidence_reaches_automatic_acceptance(self):
        controller.verification.run_verification = lambda *args, **kwargs: self.verification_report(
            overall_status="passed", coverage_status="ok", driven_project_verified=True
        )

        self.assertEqual(controller.pipeline_run(self.task, allow_dirty=True), EXIT_SUCCESS)

        state = load_state(self.task_dir, self.task)
        self.assertEqual(state["state"], "complete")
        self.assertIn("- [x] Accept", (self.task_dir / CONTRACTS["06"].filename).read_text(encoding="utf-8"))
        self.assertIn("- [x] Accept", (self.task_dir / CONTRACTS["08"].filename).read_text(encoding="utf-8"))
        self.assertTrue(state["evidence"]["verification"][-1]["source_current"])


if __name__ == "__main__":
    unittest.main()
