# F01–F05 consistency audit — low-severity findings (2026-09-25)

Audited candidate: `21d83c2` plus the uncommitted F01–F05 working tree on
`v2-cleanup`. Source: the post-implementation audit of
[v2-cleanup-followups.md](../v3prep/v2-cleanup-followups.md) against the code,
tests, and [follow-up gate record](../v3prep/v2-cleanup-followups-gate.md).

All of these findings are low severity. None of them reopens I1–I5, grants
acceptance on stale evidence, refills a budget, or blocks the follow-up gate.
They are deferred, and the audit made no runtime changes.

Validation at audit time: `python3 -m unittest discover -s agent_pipeline/tests`
ran 765 tests with one optional packaging smoke skipped, and
`python3 -m agent_pipeline.cli mock-test` passed 28 checks/scenarios. Both
match the gate record.

## Findings

| ID | Area | Finding | Evidence | Suggested action |
| --- | --- | --- | --- | --- |
| I12 | F02-FR4 / messaging | A Stage 4 gate-loop pass with `force=True` starts its local count at 0. If it exhausts the budget, it blocks with `stage_budget_exhausted_reason`, which says "a new allowance requires a change to its consumed inputs". The next forced gate pass gets attempts regardless. The wording is inaccurate only on this path, and brief F02 puts `force` handling out of scope. | `controller.ensure_real_stage` (`elif force ...: attempts_used = 0`, final exhaustion block); `gates.run_stage4_gate_loop` passes `force`. | When `force` is set, keep the old wording or add "(forced gate pass)". Revisit with V3 V10 task-wide budgets. |
| I13 | F03-FR4 / CLI | `approve-retry` now loads the config for every approval, so a broken config fails it with `EXIT_VALIDATION`, even for max-turn approvals that do not need the stage budget. This fails closed, and `run` would reject the same config. | `controller.approve_retry` calls `load_config()` before `approval_attempt_allowance`. | Load the config only when `retry_type == "failed_write_source_change"`. |
| I14 | F02-FR4 / status | `status` and `report` print `stage_attempts` whenever the task is blocked at a budgeted stage, for any reason (for example a rate limit), not only on budget exhaustion. The counts are accurate, just broader than FR4 requires. | `controller.current_stage_attempt_summary` checks only `state == "blocked"` and `last_failure.stage`. | Leave as is: the counts help diagnose any block. Optionally add an `exhausted` flag to the printed line. |
| I15 | Test hygiene | `test_f04_...hand_edited_canonical_is_retired_and_flags_mismatch` reads the archived file with a bare `open(...).read()`, which emits a `ResourceWarning` during discovery. | `agent_pipeline/tests/test_f04_invalidation_retires_promotions.py:90`. | Use `Path(...).read_text(encoding="utf-8")`. |
| I16 | F02 / identity | `current_stage_input_identity` hashes a missing consumed input as `null`, while `prepare_dispatch` would raise on a missing file. The two cannot disagree for a real dispatch (none happens without its inputs), so counts stay correct. Pre-dispatch display only. | `attempts.current_stage_input_identity` vs `attempts.prepare_dispatch`. | None needed. Note for V01 record validation. |

## Checked and not an issue

- Existing test expectations changed by F02/F03 (`test_c06_evidence`,
  `test_controller_reliability`, `test_r02_review_budget`,
  `test_r03_failed_writer_approval`, `test_r_cleanup_remediation`,
  `test_real_pipeline`) each name the F requirement that changed them. Each
  change strengthens its assertion (for example `assertNotIn("approve-retry")`
  plus a positive check on the new reason) rather than removing it.
- Approval consumption now happens inside the dispatch loop (F03-FR3). An
  approved but unconsumed approval that meets a routing failure stays usable,
  because `approved_unconsumed_here` re-enters the approval branch on the next
  `run`.
- `recover_failed_writer_approval(defer_if_upstream_stale=True)` is used only
  in the pipeline-level pass. The stage-level pass immediately before any
  writer dispatch never defers, so F03-FR2's "before any writer" holds.
- F01 `upstream_staleness` is built on `artifact_view`, the same function
  `reconcile_artifacts` now delegates to, so F01-FR2's "one shared helper"
  holds structurally, not only by test.
