"""F04 regressions: invalidation retires every promotion, including modified ones."""

from __future__ import print_function

import json
import unittest
from unittest import mock

from agent_pipeline import attempts, controller
from agent_pipeline.artifacts import sha256_file
from agent_pipeline.failures import EXIT_BLOCKED, EXIT_SUCCESS
from agent_pipeline.mock_agent import valid_artifact
from agent_pipeline.runner import atomic_finalize
from agent_pipeline.state import CONTRACTS, load_state, new_state
from agent_pipeline.tests import test_r01_recovery as r01
from agent_pipeline.tests.test_f03_no_dead_end_approval import F03Base


REVIEW = CONTRACTS["07"].filename
DECISION = CONTRACTS["08"].filename


def review_text(marker, base=None):
    """A contract-valid Stage 7 review carrying `marker` (the verdict must stay last)."""
    base = base if base is not None else valid_artifact("07")
    return base.replace("## Verdict", marker + "\n\n## Verdict", 1)


class F04Base(F03Base):
    dispatch = r01.R01RecoveryTests.dispatch
    complete = r01.R01RecoveryTests.complete
    promote = r01.R01RecoveryTests.promote

    def sidecar(self, dispatch):
        return attempts.attempts_root(self.task_dir) / dispatch["attempt_id"] / "invalidated.json"

    def load_sidecar(self, dispatch):
        return json.loads(self.sidecar(dispatch).read_text(encoding="utf-8"))

    def assert_not_resurrected(self):
        resumed = new_state(self.task, "f04-resume")
        self.assertIsNone(attempts.recover(self.task_dir, resumed, atomic_finalize, stages=("07",)))
        self.assertFalse((self.task_dir / REVIEW).exists())
        self.assertNotIn("07", resumed["real_stage_runs"])


class F04AttemptRecordBase(F04Base):
    def setUp(self):
        F04Base.setUp(self)
        (self.task_dir / CONTRACTS["06"].filename).write_text(valid_artifact("06"), encoding="utf-8")


class F04InvalidationRecordTests(F04AttemptRecordBase):
    def test_f04_fr1_fr2_matching_canonical_is_retired_without_mismatch(self):
        state = new_state(self.task, "f04-match")
        dispatch = self.dispatch(state, stage="07")
        result = self.promote(dispatch)
        promoted_hash = sha256_file(self.task_dir / REVIEW)

        archived = controller.invalidate_evidence(self.task_dir, state, ["07"], "review inputs changed")

        self.assertEqual(archived, [REVIEW])
        record = self.load_sidecar(dispatch)
        self.assertEqual(record["attempt_id"], result["attempt_id"])
        self.assertEqual(record["stage"], "07")
        self.assertEqual(record["promoted_hash"], promoted_hash)
        self.assertEqual(record["archived_hash"], promoted_hash)
        self.assertIs(record["hash_mismatch"], False)
        self.assertEqual(sha256_file(record["archived_path"]), promoted_hash)
        self.assert_not_resurrected()

    def test_f04_fr1_fr2_hand_edited_canonical_is_retired_and_flags_mismatch(self):
        state = new_state(self.task, "f04-modified")
        dispatch = self.dispatch(state, stage="07")
        self.promote(dispatch)
        promoted_hash = sha256_file(self.task_dir / REVIEW)
        edited = review_text("Operator hand edit.")
        (self.task_dir / REVIEW).write_text(edited, encoding="utf-8")
        edited_hash = sha256_file(self.task_dir / REVIEW)

        controller.invalidate_evidence(self.task_dir, state, ["07"], "review was hand-edited")

        record = self.load_sidecar(dispatch)
        self.assertIn("review was hand-edited", record["reason"])
        self.assertEqual(record["promoted_hash"], promoted_hash)
        self.assertEqual(record["archived_hash"], edited_hash)
        self.assertNotEqual(record["archived_hash"], record["promoted_hash"])
        self.assertIs(record["hash_mismatch"], True)
        # The operator's edit is preserved in history, never overwritten.
        self.assertEqual(open(record["archived_path"], encoding="utf-8").read(), edited)
        self.assert_not_resurrected()

    def test_f04_fr1_fr2_missing_canonical_is_retired_with_null_archived_hash(self):
        state = new_state(self.task, "f04-missing")
        dispatch = self.dispatch(state, stage="07")
        self.promote(dispatch)
        promoted_hash = sha256_file(self.task_dir / REVIEW)
        (self.task_dir / REVIEW).unlink()
        (self.task_dir / DECISION).write_text(valid_artifact("08"), encoding="utf-8")

        archived = controller.invalidate_evidence(self.task_dir, state, ["07", "08"], "review missing")

        self.assertEqual(archived, [DECISION])
        record = self.load_sidecar(dispatch)
        self.assertEqual(record["promoted_hash"], promoted_hash)
        self.assertIsNone(record["archived_hash"])
        self.assertIsNone(record["archived_path"])
        self.assertIsNotNone(record["history_path"])
        self.assertIs(record["hash_mismatch"], True)
        self.assert_not_resurrected()

    def test_f04_fr1_fr2_nothing_archived_still_retires_promotion(self):
        state = new_state(self.task, "f04-nothing")
        dispatch = self.dispatch(state, stage="07")
        self.promote(dispatch)
        (self.task_dir / REVIEW).unlink()

        archived = controller.invalidate_evidence(self.task_dir, state, ["07"], "review missing, nothing to archive")

        self.assertEqual(archived, [])
        record = self.load_sidecar(dispatch)
        self.assertIsNone(record["history_path"])
        self.assertIsNone(record["archived_path"])
        self.assertIsNone(record["archived_hash"])
        self.assertIs(record["hash_mismatch"], True)
        self.assert_not_resurrected()

    def test_f04_fr1_sidecar_is_written_before_any_canonical_file_is_removed(self):
        state = new_state(self.task, "f04-order")
        dispatch = self.dispatch(state, stage="07")
        self.promote(dispatch)
        (self.task_dir / REVIEW).write_text(review_text("Edited."), encoding="utf-8")
        seen = []
        real_remove = controller.evidence_module.remove

        def checked_remove(task_dir, filenames):
            seen.append((list(filenames), self.sidecar(dispatch).is_file(), (self.task_dir / REVIEW).is_file()))
            return real_remove(task_dir, filenames)

        with mock.patch.object(controller.evidence_module, "remove", side_effect=checked_remove):
            controller.invalidate_evidence(self.task_dir, state, ["07"], "ordering")

        self.assertEqual(seen, [([REVIEW], True, True)])
        self.assertFalse((self.task_dir / REVIEW).exists())

    def test_f04_fr1_every_unretired_promotion_is_retired_and_existing_records_are_immutable(self):
        state = new_state(self.task, "f04-every")
        oldest = self.dispatch(state, stage="07", attempt_number=1)
        self.promote(oldest, review_text("Oldest."))
        controller.invalidate_evidence(self.task_dir, state, ["07"], "first invalidation")
        oldest_bytes = self.sidecar(oldest).read_bytes()

        older = self.dispatch(state, stage="07", attempt_number=2)
        self.promote(older, review_text("Older."))
        newer = self.dispatch(state, stage="07", attempt_number=3)
        self.promote(newer, review_text("Newer."))
        (self.task_dir / REVIEW).write_text(review_text("Hand edit."), encoding="utf-8")

        controller.invalidate_evidence(self.task_dir, state, ["07"], "second invalidation")

        self.assertEqual(self.sidecar(oldest).read_bytes(), oldest_bytes)
        self.assertIn("first invalidation", self.load_sidecar(oldest)["reason"])
        for dispatch in (older, newer):
            record = self.load_sidecar(dispatch)
            self.assertIn("second invalidation", record["reason"])
            self.assertIs(record["hash_mismatch"], True)
        self.assert_not_resurrected()

    def test_f04_fr1_other_stages_are_untouched(self):
        state = new_state(self.task, "f04-scope")
        stage02 = self.dispatch(state, stage="02")
        self.promote(stage02)
        review = self.dispatch(state, stage="07")
        self.promote(review)

        controller.invalidate_evidence(self.task_dir, state, ["07"], "review only")

        self.assertTrue(self.sidecar(review).is_file())
        self.assertFalse(self.sidecar(stage02).exists())
        self.assertTrue((self.task_dir / CONTRACTS["02"].filename).is_file())


class F04RecoveryTests(F04AttemptRecordBase):
    def test_f04_fr3_recovery_never_restores_invalidated_hand_edited_review(self):
        state = new_state(self.task, "f04-fr3")
        dispatch = self.dispatch(state, stage="07")
        self.promote(dispatch)
        (self.task_dir / REVIEW).write_text(review_text("Hand edit."), encoding="utf-8")
        controller.invalidate_evidence(self.task_dir, state, ["07"], "hand edit")

        # Repeated recovery never re-creates the canonical artifact.
        for _ in range(2):
            self.assert_not_resurrected()

    def test_f04_c08_fr3_crash_after_promotion_is_still_adopted_once(self):
        # A proven successful attempt promoted to disk but whose controller
        # bookkeeping never ran is adopted exactly once.
        state = new_state(self.task, "f04-c08")
        dispatch = self.dispatch(state, stage="07")
        self.promote(dispatch)
        output = (self.task_dir / REVIEW).read_text(encoding="utf-8")
        resumed = new_state(self.task, "f04-c08-resume")

        self.assertIsNone(attempts.recover(self.task_dir, resumed, atomic_finalize, stages=("07",)))
        self.assertIsNone(attempts.recover(self.task_dir, resumed, atomic_finalize, stages=("07",)))

        self.assertEqual((self.task_dir / REVIEW).read_text(encoding="utf-8"), output)
        self.assertEqual([run["attempt_id"] for run in resumed["real_stage_runs"]["07"]], [dispatch["attempt_id"]])
        self.assertFalse(self.sidecar(dispatch).exists())

    def test_f04_c08_fr3_unpromoted_success_after_invalidation_is_still_adopted(self):
        # Invalidation retires promotions only; a later proven success that
        # crashed before promotion is still recovered.
        state = new_state(self.task, "f04-c08-later")
        first = self.dispatch(state, stage="07", attempt_number=1)
        self.promote(first, review_text("First."))
        (self.task_dir / REVIEW).write_text(review_text("Hand edit."), encoding="utf-8")
        controller.invalidate_evidence(self.task_dir, state, ["07"], "hand edit")
        second = self.dispatch(state, stage="07", attempt_number=2)
        second_output = review_text("Second.")
        self.complete(second, second_output)

        resumed = new_state(self.task, "f04-c08-later-resume")
        self.assertIsNone(attempts.recover(self.task_dir, resumed, atomic_finalize, stages=("07",)))

        self.assertEqual((self.task_dir / REVIEW).read_text(encoding="utf-8"), second_output)
        self.assertEqual([run["attempt_id"] for run in resumed["real_stage_runs"]["07"]], [second["attempt_id"]])
        self.assertTrue(self.sidecar(first).is_file())
        self.assertFalse(self.sidecar(second).exists())


class F04EndToEndTests(F04Base):
    def accept(self, budget):
        self.use_budget(budget)
        self.env(FAKE_COUNT_PATH=str(self.root / "counts.txt"))
        controller.verification.run_verification = lambda *args, **kwargs: self.verification_report(
            overall_status="passed", coverage_status="ok", driven_project_verified=True
        )
        code, state = self.pipeline()
        self.assertEqual(code, EXIT_SUCCESS)
        self.assertEqual(state["state"], "complete")
        self.assertEqual(self.dispatches("07"), 1)
        review = self.task_dir / REVIEW
        original = review.read_text(encoding="utf-8")
        edited = review_text("Operator hand edit after acceptance.", original)
        review.write_text(edited, encoding="utf-8")
        return original, edited

    def review_sidecars(self):
        records = []
        for dispatch in self.dispatch_records("07"):
            path = self.sidecar(dispatch)
            if path.is_file():
                records.append(json.loads(path.read_text(encoding="utf-8")))
        return records

    def test_f04_acceptance_budget1_hand_edit_is_not_reverted_and_blocks_on_exhaustion(self):
        original, edited = self.accept(1)

        code, state = self.pipeline()

        self.assertEqual(code, EXIT_BLOCKED)
        review = self.task_dir / REVIEW
        # The original promoted review is never restored over the edit.
        self.assertFalse(review.exists() and review.read_text(encoding="utf-8") == original)
        self.assertEqual(self.dispatches("07"), 1)
        reason = state["last_failure"]["reason"]
        self.assertIn("Stage 7 attempt budget exhausted for review-input identity", reason)
        self.assertIn("a new allowance requires a source, review-input, review-config, or bound Stage 6 change", reason)
        self.assertNotIn("approve-retry", reason)
        records = self.review_sidecars()
        self.assertEqual(len(records), 1)
        self.assertIs(records[0]["hash_mismatch"], True)
        self.assertEqual(open(records[0]["archived_path"], encoding="utf-8").read(), edited)

        # A resume does not resurrect it either.
        code, state = self.pipeline()
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertFalse(review.exists() and review.read_text(encoding="utf-8") == original)
        self.assertEqual(self.dispatches("07"), 1)

    def test_f04_acceptance_budget2_hand_edit_produces_a_new_review(self):
        _, edited = self.accept(2)

        code, state = self.pipeline()

        self.assertEqual(code, EXIT_SUCCESS)
        self.assertEqual(state["state"], "complete")
        self.assertEqual(self.dispatches("07"), 2)
        review = self.task_dir / REVIEW
        self.assertNotEqual(review.read_text(encoding="utf-8"), edited)
        first, second = self.dispatch_records("07")
        # The canonical review is the second attempt's own promotion, not a
        # restore of the retired first one.
        self.assertTrue(self.sidecar(first).is_file())
        self.assertFalse(self.sidecar(second).exists())
        promoted = json.loads((self.sidecar(second).parent / "promoted.json").read_text(encoding="utf-8"))
        self.assertEqual(promoted["final_hash"], sha256_file(review))
        self.assertEqual(state["real_stage_runs"]["07"][-1]["attempt_id"], second["attempt_id"])
        self.assertEqual(len(self.review_sidecars()), 1)
        self.assertIn("- [x] Accept", (self.task_dir / DECISION).read_text(encoding="utf-8"))


class F04AutomaticAcceptanceGuardTests(F04Base):
    def test_f04_a1_invalid_evidence_stays_ineligible_and_valid_path_accepts(self):
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

        # A valid run reaches automatic acceptance and retires nothing.
        self.use_budget(1)
        controller.verification.run_verification = lambda *args, **kwargs: self.verification_report(
            overall_status="passed", coverage_status="ok", driven_project_verified=True
        )
        code, state = self.pipeline()
        self.assertEqual(code, EXIT_SUCCESS)
        self.assertEqual(state["state"], "complete")
        self.assertIn("- [x] Accept", (self.task_dir / DECISION).read_text(encoding="utf-8"))
        root = attempts.attempts_root(self.task_dir)
        self.assertEqual([path for path in root.glob("*/invalidated.json")], [])
        accepted = load_state(self.task_dir, self.task)
        self.assertTrue(accepted["evidence"]["verification"][-1]["source_current"])


if __name__ == "__main__":
    unittest.main()
