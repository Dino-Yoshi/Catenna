# V2 cleanup follow-ups — Implementation brief

Status: F01–F05 implemented in the working tree on `v2-cleanup`; the [follow-up gate](v2-cleanup-followups-gate.md) stays open until the candidate is committed and re-tested at that commit. Source: the 2026-09-25 consistency audit of R01–R08 against commit `21d83c2` (branch `v2-cleanup`). This brief covers the High and Medium findings I1–I5. Low findings are recorded separately in [2026-09-25-r01-r08-low-findings.md](../audits/2026-09-25-r01-r08-low-findings.md) and are out of scope here.

Purpose: fix residual cross-slice gaps before V3. The [V2 cleanup gate](v2-cleanup-gate.md) stays closed for C01–C09 and R01–R08. This brief has its own gate (F05), which must close before any [V3 milestone](v3.md) begins. Deliver F01–F05 in order. F03 depends on F02, and F05 runs last. Requirement IDs are stable identifiers for implementation tasks and regression tests.

Common rules for every slice:

- Implement only the named slice. Do not start another slice or any V3 milestone.
- Every fix needs a regression test that fails against `21d83c2` and passes after the fix. Tests use real temporary Git working trees and real local subprocesses where the defect involves them, and fake agents only. No paid provider calls.
- Do not weaken, skip, or delete existing assertions to make tests pass. If an existing test's expectation contradicts a requirement below, change it and name the requirement in the test.
- Do not rewrite the controller wholesale or change storage technology. Extend the existing durable records (`attempts.py`, `durable.py`, `state.json`).
- Stop and report instead of guessing when a material design choice is not settled here.
- Finish each slice with `python3 -m unittest discover -s agent_pipeline/tests` and `python3 -m agent_pipeline.cli mock-test`. Both must pass; the C/R gate tests must stay green throughout.

Settled operator decisions (2026-09-25):

1. When no attempt can follow, Catenna does not create or consume a retry approval; it blocks with a reason that says how a new allowance is obtained. An approval never refills a budget (C03-FR4 and R02 stand as written).
2. Every agent stage receives `stage_attempt_budget` attempts once per consumed-input identity, counted from durable dispatch records, as R02 already does for Stage 7 review-input identities.
3. These slices form a separate follow-up gate that must close before V3 milestone 1. The C/R gate record is not reopened.
4. Unbounded `state.json` growth from per-path identity fingerprints (audit I11) is deferred to V3 slice V01, not fixed here.

## Slice F01 — Current acceptance respects the full upstream cascade

**Problem description / scope.** Audit I1. `evidence.evaluate` (`agent_pipeline/evidence.py`, the `changed_seed` loop) compares only `00_original_request.md` and `01_requirements_packet.md` with their acknowledged hashes. After acceptance, an edit to `02_*` or `03_*` leaves `status` at `current_acceptance: yes`, while the same output shows `state: ready` and `current_stage: 03`, and `run` re-dispatches Stage 03. Scope: the read-only current-eligibility evaluation and the final acceptance boundary. Traces to C06-FR4 and the R01 acceptance criteria.

**Functional requirements**

- F01-FR1: Current-acceptance evaluation shall treat Stage 6–8 evidence as not current when any upstream task artifact consumed by the accepted chain (`00`, `01`, `02`, `03`, `04`, `04_gate`, `05`) is missing or differs from its acknowledged hash.
- F01-FR2: That staleness shall come from the same rule `reconcile_artifacts` uses (one shared read-only helper), not from a second hard-coded list.
- F01-FR3: The reason shall name the changed artifacts and the first stage `run` will re-dispatch.
- F01-FR4: `status`, `dry-run`, `report`, and `ensure_current_decision` shall use this check, so `current_acceptance` is never `yes` while `current_stage` precedes `06`.

**Constraints.** Evaluation stays read-only. Do not change the `state.invalidated_from` cascade or when inputs are acknowledged.

**Acceptance.** After acceptance, editing any of `00`–`03` makes `status` report `current_acceptance: no` and name the file. The next `run` re-dispatches from the following stage. When the re-dispatched outputs are unchanged, acceptance returns to `yes`. An unedited accepted task stays `yes`.

## Slice F02 — Stage budget per consumed-input identity

**Problem description / scope.** Audit I5 and operator decision 2. R01 added an unspecified fresh budget: `attempts.recover` computes `_r01_fresh_input_stages`, and `ensure_real_stage` resets `attempts_used` for listed stages. The allowance lives only in `state.json` and is lost after one failed current-input attempt. A resume then falls back to the lifetime `state["attempts"]` count. Scope: attempt accounting for stages `02`, `03`, `04`, `04_gate`, and `05`. Stage 7 keeps its R02 review-input identity.

**Functional requirements**

- F02-FR1: Define a stage input identity as a digest of the stage key and the dispatch-time `consumed_input_hashes`. Record it in every dispatch record for these stages.
- F02-FR2: Each distinct stage input identity receives `stage_attempt_budget` attempts once. Attempts used per identity shall be counted from durable dispatch records, like `count_stage7_attempts`. Resume, recovery, and invalidation shall not replenish them. A crash after dispatch does not refund the attempt.
- F02-FR3: Remove the `_r01_fresh_input_stages` marker and its in-memory allowance. `state["attempts"]` remains the attempt-numbering and history counter only, not the budget.
- F02-FR4: A budget-exhaustion block names the stage and its input identity. `status` and `report` show attempts used and allowed for a stage blocked on its budget.
- F02-FR5: Dispatches without `consumed_input_hashes` count toward no identity. The R01-FR5 block on adopting them is unchanged.

**Constraints.** Keep the semantics of completion retries and approved non-writer retries. The Stage 4 gate loop's `force` handling stays out of scope. Do not introduce task-wide budgets (V3 V10).

**Acceptance.** With budget 1, an upstream edit grants exactly one dispatch of the affected stage. If that attempt fails, repeated `run`, resume, or a crash after dispatch does not grant another. A further edit grants exactly one more. Unchanged inputs never receive more than the budget.

## Slice F03 — No approval when no attempt can follow

**Problem description / scope.** Audit I2 and I3, and operator decision 1. Two routes lead to a dead end:

- A failed Stage 5 writer that changed source on its last in-budget attempt still creates an approval. After the operator approves it, `ensure_real_stage` consumes it and blocks with "attempt budget exhausted" without dispatching.
- A Stage 7 review identity with no attempts left blocks with "use approve-retry", but no pending approval exists.

Scope: `failed_writer_requires_approval`, `recover_failed_writer_approval`, approval consumption in `ensure_real_stage`, and the Stage 7 exhaustion reason. Depends on F02. This replaces the deferred R02 follow-up recorded in the gate record.

**Functional requirements**

- F03-FR1: Before creating a failed-writer approval, live or during recovery, the controller shall check whether the stage input identity has an attempt left. If none remains, it shall not create a pending approval. It shall block with a reason that names the changed or uncertain paths, states that further writers are blocked, and explains that a new allowance requires a changed consumed input.
- F03-FR2: A failed writer that changed source and has no allowance stays unresolved in its durable records. When a later identity has an allowance, recovery shall recreate the source-bound approval requirement before any writer is dispatched (C03-FR4).
- F03-FR3: An approval is consumed only in the step that dispatches an attempt. If no attempt can be dispatched, the approval stays unconsumed.
- F03-FR4: An existing unconsumed pending approval whose stage has no remaining attempt shall be marked withdrawn durably and kept in history. The task then blocks with the exhaustion reason.
- F03-FR5: The Stage 7 exhaustion reason shall name the review-input identity. It shall say a new allowance requires a source, review-input, review-config, or bound Stage 6 change, and it shall not mention `approve-retry`.

**Constraints.** Approval never refills a budget. Human-approved max-turn retries keep their existing semantics. Never reset or revert user files.

**Acceptance.**

- With budget 1, a failed writer that changes source blocks without a pending approval and without consuming one, and the reason names the path.
- After a brief change, the new identity requires source-bound approval before any writer runs.
- With budget 2, a first failure still leads to approval and a second writer.
- An exhausted Stage 7 identity never suggests `approve-retry`. `approve-retry` and `USAGE.md` agree.

## Slice F04 — Invalidation retires modified promotions

**Problem description / scope.** Audit I4. `attempts.invalidate_promoted` writes `invalidated.json` only when the archived canonical file's hash equals the promotion's `final_hash`. If `07_diff_review.md` was hand-edited after promotion, invalidation leaves the attempt unmarked. `recover` then re-promotes the original review and restores the file. With budget 1, the task stays blocked with the restored review on disk. Scope: `invalidate_promoted` and its call in `controller.invalidate_evidence`. Traces to R01-FR3 and C08-FR3.

**Functional requirements**

- F04-FR1: When `invalidate_evidence` invalidates a stage, every promoted attempt of that stage without an invalidation record shall receive `invalidated.json` before any canonical file is removed. This applies whether the canonical file matches, differs from, or is missing relative to the promotion.
- F04-FR2: The invalidation record shall include the promoted hash and the archived hash (or `null` when nothing was archived), and flag a mismatch.
- F04-FR3: Recovery shall never re-create a canonical artifact from an attempt that has an invalidation record (unchanged R01-FR3 behavior, now reachable in every case).

**Constraints.** Sidecars remain immutable; existing `invalidated.json` files are not rewritten. Recovery still adopts a proven successful attempt once after a crash between promotion and bookkeeping (C08-FR3).

**Acceptance.** Hand-edit `07_diff_review.md` after acceptance and run. With budget 1, the original review is not restored, and the task blocks with the F03 exhaustion reason. With budget 2, a new review is produced. The existing crash-after-promotion regressions still pass.

## Slice F05 — Follow-up gate

**Problem description / scope.** F01–F04 must close with permanent regressions before V3 begins. Scope: a new gate record, gate tests, and documentation.

**Functional requirements**

- F05-FR1: Convert the 2026-09-25 audit probes into permanent tests with inverted expectations, one or more per finding I1–I5, in a new module such as `agent_pipeline/tests/test_f_followups.py`. The probes covered upstream edits to `02`/`03`, failed-writer approval at budget exhaustion, and invalidation of a hand-edited review.
- F05-FR2: Create `docs/v3prep/v2-cleanup-followups-gate.md`. Record the tested commit (not a working tree), date, environment, integration results, a `PASS` row per F requirement, and a finding table showing I1–I5 closed. Add gate tests that require every F row to be `PASS` and every cited test to load and pass. The existing C/R gate tests must continue to pass unchanged.
- F05-FR3: Amend [v2-cleanup.md](v2-cleanup.md) C03-FR4 and C06-FR3 wording to reflect per-input-identity budgets and no approval at exhaustion. Mark the deferred approve-retry item in [v2-cleanup-gate.md](v2-cleanup-gate.md) as resolved by F03, with a link.
- F05-FR4: Update `docs/USAGE.md` for per-input-identity budgets and exhaustion without approval.
- F05-FR5: Refresh the stale status lines in `v2-cleanup-remediation.md`, `v2-cleanup.md`, and `srs-review.md`. Change the entry gate in [v3.md](v3.md) to require both the C09 gate and this follow-up gate. Record audit I11 (per-path fingerprints growing `state.json`) as an input to V01.

**Constraints.** The gate stays open while any required test is skipped or failing, or while the tested revision is uncommitted. No paid providers.

**Acceptance.** Both required suites pass with only the optional packaging smoke skipped. Every I1–I5 has a passing regression, and the follow-up gate record shows `closed` against a commit. Only then may V3 milestone 1 begin.
