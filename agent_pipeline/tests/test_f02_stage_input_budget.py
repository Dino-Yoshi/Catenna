"""F02 regressions: stage budgets are granted once per consumed-input identity."""

from __future__ import print_function

import hashlib
import io
import json
import os
import unittest
from contextlib import redirect_stdout

from agent_pipeline import attempts, controller
from agent_pipeline.failures import EXIT_BLOCKED, EXIT_SUCCESS
from agent_pipeline.mock_agent import valid_artifact
from agent_pipeline.runner import atomic_finalize
from agent_pipeline.state import CONTRACTS, STAGE_CONSUMED_INPUTS, load_state, new_state, state_path
from agent_pipeline.tests import test_real_pipeline as base


def expected_identity(stage, consumed_input_hashes):
    payload = {"stage": stage, "consumed_input_hashes": consumed_input_hashes}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class F02Base(unittest.TestCase):
    setUp = base.RealPipelineTests.setUp
    tearDown = base.RealPipelineTests.tearDown
    config = base.RealPipelineTests.config
    write_fake_agent = base.RealPipelineTests.write_fake_agent
    verification_report = base.RealPipelineTests.verification_report

    def use_budget(self, budget):
        cfg = dict(self.config(), stage_attempt_budget=budget)
        controller.load_config = lambda: cfg
        return cfg

    def enable_valid_automatic_acceptance(self):
        controller.verification.run_verification = lambda *args, **kwargs: self.verification_report(
            overall_status="passed", coverage_status="ok", driven_project_verified=True
        )

    def accepted_task(self):
        self.enable_valid_automatic_acceptance()
        self.assertEqual(controller.pipeline_run(self.task, allow_dirty=True), EXIT_SUCCESS)
        state = load_state(self.task_dir, self.task)
        self.assertEqual(state["state"], "complete")
        return state

    def count_dispatches(self, stage):
        self.count_path = self.root / "counts.txt"
        if not self.count_path.exists():
            return 0
        return self.count_path.read_text(encoding="utf-8").splitlines().count(stage)

    def start_counting_and_fail(self, stage):
        os.environ["FAKE_COUNT_PATH"] = str(self.root / "counts.txt")
        os.environ["FAKE_FAIL_STAGE"] = stage
        self.addCleanup(lambda: os.environ.pop("FAKE_COUNT_PATH", None))
        self.addCleanup(lambda: os.environ.pop("FAKE_FAIL_STAGE", None))

    def edit_request(self, text):
        (self.task_dir / CONTRACTS["00"].filename).write_text(
            "# Original request\n\n%s\n" % text, encoding="utf-8"
        )

    def run_blocked(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(controller.pipeline_run(self.task, allow_dirty=True), EXIT_BLOCKED)
        return load_state(self.task_dir, self.task)

    def dispatch_records(self, stage=None):
        records = []
        for directory in attempts.attempts_root(self.task_dir).iterdir():
            path = directory / "dispatch.json"
            if path.is_file():
                record = json.loads(path.read_text(encoding="utf-8"))
                if stage is None or record.get("stage") == stage:
                    records.append(record)
        return records

    def manual_dispatch(self, state, stage="02", attempt_kind="normal", attempt_number=99):
        prompt = self.task_dir / ("f02-%s-%s.prompt.md" % (stage, attempt_number))
        prompt.write_text("prompt\n", encoding="utf-8")
        return attempts.prepare_dispatch(
            self.task_dir, state, stage, "codex", "read-only", 1, attempt_number,
            attempt_kind, "test", prompt, {}, state.get("dirty_baseline"), {"model": "fake"},
            review_input_identity=("0" * 64 if stage == "07" else None),
        )


class F02IdentityRecordTests(F02Base):
    def test_f02_fr1_every_budgeted_dispatch_records_its_stage_input_identity(self):
        self.accepted_task()
        seen = set()
        for record in self.dispatch_records():
            stage = record["stage"]
            if stage not in attempts.BUDGETED_STAGES:
                self.assertNotIn("stage_input_identity", record)
                continue
            seen.add(stage)
            expected_names = {CONTRACTS[key].filename for key in STAGE_CONSUMED_INPUTS[stage]}
            self.assertEqual(set(record["consumed_input_hashes"]), expected_names)
            self.assertEqual(
                record["stage_input_identity"],
                expected_identity(stage, record["consumed_input_hashes"]),
            )
        self.assertEqual(seen, set(attempts.BUDGETED_STAGES))

    def test_f02_fr1_identity_binds_stage_key_and_consumed_hashes(self):
        hashes = {"x.md": "a" * 64}
        self.assertNotEqual(
            attempts.stage_input_identity("03", hashes),
            attempts.stage_input_identity("04", hashes),
        )
        self.assertNotEqual(
            attempts.stage_input_identity("03", hashes),
            attempts.stage_input_identity("03", {"x.md": "b" * 64}),
        )
        # Stage 7 keeps its R02 review-input identity; F02 does not apply.
        self.assertIsNone(attempts.stage_input_identity("07", hashes))
        state = new_state(self.task, "f02-fr1")
        (self.task_dir / CONTRACTS["06"].filename).write_text(valid_artifact("06"), encoding="utf-8")
        review = self.manual_dispatch(state, stage="07")
        self.assertNotIn("stage_input_identity", review)
        self.assertEqual(review["review_input_identity"], "0" * 64)
        dispatch = self.manual_dispatch(state, stage="02")
        self.assertEqual(dispatch["stage_input_identity"], attempts.current_stage_input_identity(self.task_dir, "02"))


class F02BudgetTests(F02Base):
    def test_f02_fr2_budget1_upstream_edit_grants_exactly_one_dispatch_per_identity(self):
        self.accepted_task()
        self.start_counting_and_fail("02")

        self.edit_request("First edit.")
        first_identity = attempts.current_stage_input_identity(self.task_dir, "02")
        self.run_blocked()
        self.assertEqual(self.count_dispatches("02"), 1)

        # Repeated run and a fresh-process resume grant nothing more.
        self.run_blocked()
        self.run_blocked()
        self.assertEqual(self.count_dispatches("02"), 1)
        self.assertEqual(attempts.count_stage_input_attempts(self.task_dir, "02", first_identity), 1)

        # A further edit is a new identity and gets exactly one more.
        self.edit_request("Second edit.")
        second_identity = attempts.current_stage_input_identity(self.task_dir, "02")
        self.assertNotEqual(first_identity, second_identity)
        self.run_blocked()
        self.run_blocked()
        self.assertEqual(self.count_dispatches("02"), 2)

        # Reverting to an exhausted identity (an invalidation back to earlier
        # inputs) does not replenish its budget.
        self.edit_request("First edit.")
        self.assertEqual(attempts.current_stage_input_identity(self.task_dir, "02"), first_identity)
        state = self.run_blocked()
        self.assertEqual(self.count_dispatches("02"), 2)
        self.assertIn(first_identity, state["last_failure"]["reason"])

    def test_f02_fr2_budget2_failed_attempt_leaves_one_more_for_the_same_identity(self):
        # Against 21d83c2 the R01 in-memory allowance was lost after the first
        # failed current-input attempt, so the resume fell back to the lifetime
        # counter and granted nothing.
        self.accepted_task()
        self.use_budget(2)
        self.start_counting_and_fail("02")
        self.edit_request("Edit under budget two.")
        identity = attempts.current_stage_input_identity(self.task_dir, "02")

        self.run_blocked()
        self.assertEqual(self.count_dispatches("02"), 1)
        self.run_blocked()
        self.assertEqual(self.count_dispatches("02"), 2)
        state = self.run_blocked()
        self.assertEqual(self.count_dispatches("02"), 2)
        self.assertEqual(attempts.count_stage_input_attempts(self.task_dir, "02", identity), 2)
        self.assertIn("stage input identity " + identity, state["last_failure"]["reason"])
        # Numbering history still counts every attempt.
        self.assertEqual(state["attempts"]["02"], 3)

    def test_f02_fr2_unchanged_inputs_never_exceed_the_budget(self):
        self.use_budget(2)
        self.start_counting_and_fail("02")
        for _ in range(4):
            self.run_blocked()
        self.assertEqual(self.count_dispatches("02"), 2)

    def test_f02_fr2_crash_after_dispatch_does_not_refund_the_attempt(self):
        state = self.accepted_task()
        self.start_counting_and_fail("02")
        self.edit_request("Edit before crash.")
        identity = attempts.current_stage_input_identity(self.task_dir, "02")
        self.assertEqual(attempts.count_stage_input_attempts(self.task_dir, "02", identity), 0)

        # The controller persisted the dispatch and died before completion.
        crashed = self.manual_dispatch(state, attempt_number=state["attempts"]["02"] + 1)
        self.assertEqual(crashed["stage_input_identity"], identity)
        self.assertEqual(attempts.count_stage_input_attempts(self.task_dir, "02", identity), 1)
        self.run_blocked()
        self.assertEqual(self.count_dispatches("02"), 0)

        # Even once its failure is durably recorded, the allowance stays spent.
        attempts.record_completion(
            self.task_dir, {"attempt_id": crashed["attempt_id"], "stage": "02", "status": "failed"},
            "# interrupted\n", {"valid": False}, False,
        )
        state = self.run_blocked()
        self.assertEqual(self.count_dispatches("02"), 0)
        self.assertIn("Stage 02 attempt budget exhausted for stage input identity " + identity, state["last_failure"]["reason"])

    def test_f02_fr2_completion_retries_are_not_charged_to_the_identity(self):
        state = new_state(self.task, "f02-completion")
        identity = attempts.current_stage_input_identity(self.task_dir, "02")
        self.manual_dispatch(state, attempt_number=1)
        self.manual_dispatch(state, attempt_kind="completion_only_retry", attempt_number=2)
        self.assertEqual(attempts.count_stage_input_attempts(self.task_dir, "02", identity), 1)


class F02MarkerRemovalTests(F02Base):
    def test_f02_fr3_legacy_fresh_marker_grants_nothing_and_is_dropped(self):
        self.accepted_task()
        self.start_counting_and_fail("02")
        self.edit_request("Exhaust this identity.")
        self.run_blocked()
        self.assertEqual(self.count_dispatches("02"), 1)

        path = state_path(self.task_dir)
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored["_r01_fresh_input_stages"] = ["02", "03"]
        path.write_text(json.dumps(stored, sort_keys=True) + "\n", encoding="utf-8")

        state = self.run_blocked()
        self.assertEqual(self.count_dispatches("02"), 1)
        self.assertNotIn("_r01_fresh_input_stages", state)

    def test_f02_fr3_recover_writes_no_allowance_marker(self):
        state = new_state(self.task, "f02-fr3")
        dispatch = self.manual_dispatch(state, attempt_number=1)
        attempts.record_completion(
            self.task_dir, {"attempt_id": dispatch["attempt_id"], "stage": "02", "status": "failed"},
            "# failed\n", {"valid": False}, False,
        )
        self.edit_request("Stale the recorded attempt.")
        resumed = new_state(self.task, "f02-fr3-resume")
        self.assertIsNone(attempts.recover(self.task_dir, resumed, atomic_finalize))
        self.assertNotIn("_r01_fresh_input_stages", resumed)

    def test_f02_fr3_lifetime_counter_is_numbering_only(self):
        self.use_budget(1)
        self.start_counting_and_fail("NONE")
        stored = new_state(self.task, "f02-lifetime")
        stored["attempts"]["02"] = 50
        path = state_path(self.task_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(stored, sort_keys=True) + "\n", encoding="utf-8")

        with redirect_stdout(io.StringIO()):
            controller.pipeline_run(self.task, allow_dirty=True)
        self.assertEqual(self.count_dispatches("02"), 1)
        record = self.dispatch_records("02")[0]
        self.assertEqual(record["attempt_number"], 51)


class F02VisibilityTests(F02Base):
    def test_f02_fr4_exhaustion_block_names_stage_identity_in_status_and_report(self):
        self.accepted_task()
        self.start_counting_and_fail("03")
        (self.task_dir / CONTRACTS["02"].filename).write_text(
            valid_artifact("02") + "\nOperator edit.\n", encoding="utf-8"
        )
        identity = attempts.current_stage_input_identity(self.task_dir, "03")
        self.run_blocked()
        state = self.run_blocked()
        self.assertEqual(self.count_dispatches("03"), 1)
        reason = state["last_failure"]["reason"]
        self.assertEqual(state["last_failure"]["stage"], "03")
        self.assertIn("Stage 03 attempt budget exhausted", reason)
        self.assertIn("stage input identity " + identity, reason)
        self.assertIn("1/1 attempts used", reason)
        self.assertIn(CONTRACTS["02"].filename, reason)

        status_output = io.StringIO()
        with redirect_stdout(status_output):
            self.assertEqual(controller.status(self.task), EXIT_SUCCESS)
        self.assertIn("stage_attempts: 03 1/1 (stage input identity %s)" % identity, status_output.getvalue())

        report_output = io.StringIO()
        with redirect_stdout(report_output):
            self.assertEqual(controller.pipeline_report(self.task), EXIT_SUCCESS)
        self.assertIn("Stage 03 attempts: 1/1 used (stage input identity %s)" % identity, report_output.getvalue())
        structured = json.loads((self.task_dir / ".orchestrator" / "task_report.json").read_text(encoding="utf-8"))
        self.assertEqual(structured["stage_attempts"], {
            "stage": "03", "identity": identity, "attempts_used": 1,
            "attempts_allowed": 1, "exhausted": True,
        })

    def test_f02_fr4_unblocked_task_shows_no_stage_budget(self):
        self.accepted_task()
        status_output = io.StringIO()
        with redirect_stdout(status_output):
            controller.status(self.task)
        self.assertNotIn("stage_attempts:", status_output.getvalue())


class F02LegacyProvenanceTests(F02Base):
    def test_f02_fr5_dispatch_without_consumed_hashes_counts_toward_no_identity(self):
        state = new_state(self.task, "f02-fr5")
        identity = attempts.current_stage_input_identity(self.task_dir, "02")
        dispatch = self.manual_dispatch(state, attempt_number=1)
        path = attempts.attempts_root(self.task_dir) / dispatch["attempt_id"] / "dispatch.json"
        legacy = json.loads(path.read_text(encoding="utf-8"))
        legacy.pop("consumed_input_hashes")
        path.write_text(json.dumps(legacy, sort_keys=True) + "\n", encoding="utf-8")

        self.assertIsNone(attempts.stage_input_identity("02", None))
        self.assertEqual(attempts.count_stage_input_attempts(self.task_dir, "02", identity), 0)

    def test_f02_fr5_r01_fr5_block_on_adopting_legacy_success_is_unchanged(self):
        state = new_state(self.task, "f02-fr5-adopt")
        dispatch = self.manual_dispatch(state, attempt_number=1)
        output = valid_artifact("02")
        candidate = self.task_dir / "legacy.candidate.md"
        candidate.write_text(output, encoding="utf-8")
        attempts.record_completion(
            self.task_dir,
            {"attempt_id": dispatch["attempt_id"], "stage": "02", "status": "passed",
             "candidate_artifact_path": str(candidate), "postcondition": {"valid": True}},
            output, {"valid": True}, True,
        )
        path = attempts.attempts_root(self.task_dir) / dispatch["attempt_id"] / "dispatch.json"
        legacy = json.loads(path.read_text(encoding="utf-8"))
        legacy.pop("consumed_input_hashes")
        path.write_text(json.dumps(legacy, sort_keys=True) + "\n", encoding="utf-8")

        reason = attempts.recover(self.task_dir, new_state(self.task, "resume"), atomic_finalize, stages=("02",))
        self.assertIn(dispatch["attempt_id"], reason)
        self.assertIn("lacks dispatch-time input provenance", reason)
        self.assertFalse((self.task_dir / CONTRACTS["02"].filename).exists())


class F02AutomaticAcceptanceGuardTests(F02Base):
    def test_f02_a1_invalid_evidence_stays_ineligible_and_valid_path_accepts(self):
        handoff = {"route": "manual_test"}
        invalid = (
            (dict(self.config(), enable_auto_verified=False), self.verification_report(overall_status="passed", coverage_status="ok", driven_project_verified=True)),
            (self.config(), self.verification_report(overall_status="failed", coverage_status="ok", driven_project_verified=False)),
            (self.config(), self.verification_report(overall_status="passed", coverage_status="flagged", driven_project_verified=True)),
            (self.config(), self.verification_report()),
        )
        for config, report in invalid:
            with self.subTest(report=report):
                eligibility = controller.automatic_verification_eligibility(config, report, handoff)
                self.assertFalse(eligibility["eligible"])
                self.assertTrue(eligibility["reason"])

        # Budget 1 per identity still lets a valid run reach automatic
        # acceptance with exactly one dispatch per budgeted stage.
        state = self.accepted_task()
        self.assertTrue(state["evidence"]["verification"][-1]["source_current"])
        self.assertIn("- [x] Accept", (self.task_dir / CONTRACTS["08"].filename).read_text(encoding="utf-8"))
        for stage in attempts.BUDGETED_STAGES:
            with self.subTest(stage=stage):
                self.assertEqual(len(self.dispatch_records(stage)), 1)


if __name__ == "__main__":
    unittest.main()
