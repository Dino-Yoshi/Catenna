# V2 cleanup remediation — Implementation brief

Status: implemented; the [cleanup completion gate](v2-cleanup-gate.md) closed 2026-09-25 after R08. Residual cross-slice gaps are addressed by [follow-up slices F01–F05](v2-cleanup-followups.md). Source: 2026-09-25 review of the C01–C09 working-tree candidate; defects D1–D8 are recorded in the [cleanup completion gate](v2-cleanup-gate.md#reopened-defects). Normative requirements: [v2-cleanup.md](v2-cleanup.md), whose amended C03/C05/C06/C07/C08 wording these slices implement.

Purpose: close the cleanup gate. Each slice fixes one class of defect found where C01–C09 interact. Deliver R01–R08 in order; R02 depends on R01, and R08 runs last. Requirement IDs are stable identifiers for implementation tasks and regression tests.

Common rules for every slice:

- Implement only the named slice. Do not start another slice or any V3 milestone.
- Every fix needs a regression test that fails against the current candidate and passes after the fix. Tests use real temporary Git working trees and real local subprocesses where the defect involves them, and fake agents only. No paid provider calls.
- Do not weaken, skip, or delete existing assertions to make tests pass. If an existing test's expectation contradicts the amended SRS, change it and name the requirement in the test.
- Do not rewrite the controller wholesale or change storage technology. Extend the existing durable records (`attempts.py`, `durable.py`, `state.json`).
- Stop and report instead of guessing when a material design choice is not settled here.
- Finish each slice with `python3 -m unittest discover -s agent_pipeline/tests` and `python3 -m agent_pipeline.cli mock-test`. The one expected failure until R08 is `test_c09_fr4_every_requirement_maps_to_loadable_passing_evidence`, because the gate record is deliberately open.

Settled operator decisions (2026-09-25):

1. When driven-project checks are configured, they must pass for both automatic and manual Stage 6 acceptance. Manual-only acceptance remains available when none are configured.
2. A Stage 7 re-review gets the normal stage attempt budget once per new review-input identity, persisted so resumes cannot refill it.
3. Source identity stays strict: no configurable exclusions. Stale-evidence reasons name the changed paths; ignore-rule setup is deferred to V3 `init`/`doctor`.

## Slice R01 — Recovery respects consumed inputs and invalidation

**Problem description / scope.** D1 and D2 (resurrection). `attempts.recover` (`agent_pipeline/attempts.py:103`) runs at the start of every `run_real_pipeline` and inside every `ensure_real_stage`. It calls `acknowledge_consumed_inputs` for every successful attempt, rewriting `state["input_hashes"]` from the current files. Editing `00_original_request.md` after acceptance therefore no longer invalidates anything, and the task stays accepted without new agent calls. It also re-promotes Stage 7 reviews that `invalidate_evidence` (`agent_pipeline/controller.py:1017`) archived and removed. As a result, the second re-review of a task fails with "conflicting successful durable attempts". Scope: dispatch records, invalidation bookkeeping, and `recover`. Traces to C06 constraints, C06-FR3, and C08-FR3/FR4.

**Functional requirements**

- R01-FR1: `prepare_dispatch` shall record the SHA-256 of each consumed input artifact for the stage (`state.STAGE_CONSUMED_INPUTS`) as read at dispatch time, not the previously acknowledged `state["input_hashes"]`.
- R01-FR2: Recovery shall adopt a successful attempt only while every consumed input still matches its dispatch-time hash. When adopting, it shall acknowledge those recorded hashes. It shall never derive acknowledged hashes from the current files. A mismatched attempt is stale: it is neither promoted nor acknowledged, and the stage stays incomplete.
- R01-FR3: `invalidate_evidence` shall durably mark each attempt whose promoted result it archives (for example, an immutable `invalidated.json` beside `promoted.json` recording reason and history path) before removing the canonical file. Recovery shall treat invalidated attempts as historical: never promoted, never counted as successful, and never a conflict.
- R01-FR4: An attempt superseded by a later promoted attempt of the same stage is historical. Conflict detection shall consider only non-historical successful attempts.
- R01-FR5: A task whose attempts predate R01-FR1 (no dispatch-time consumed hashes) shall not be adopted automatically. Recovery shall block with a reason that names the attempt and says it lacks input provenance, rather than guessing.

**Constraints.** Dispatch and invalidation records stay immutable; add records rather than editing old ones. Recovery must still adopt a proven successful attempt once when its inputs are unchanged (existing C08 behavior). Do not change the upstream cascade in `state.invalidated_from`.

**Acceptance.** Editing `00_original_request.md` or `01_requirements_packet.md` after acceptance makes `status` report `current_acceptance: no`, and the next `run` re-dispatches from Stage 02. Two successive review-config changes each produce one new review, with no conflict block and no resurrected `07_diff_review.md` after invalidation. A crash after promotion but before bookkeeping still resumes without another agent call.

## Slice R02 — Bounded re-review allowance

**Problem description / scope.** D2 (budget). `ensure_current_review` (`agent_pipeline/controller.py:1079`) passes `force=True` whenever the latest Stage 7 record is `invalidated`, which resets `attempts_used` to zero. Combined with R01's resurrection bug, a failing re-review is re-dispatched to the paid reviewer on every resume, even with `stage_attempt_budget: 1`. Scope: Stage 7 attempt accounting. Depends on R01. Implements operator decision 2 and C06-FR3.

**Functional requirements**

- R02-FR1: Define the review-input identity as a digest of the current source fingerprint, the `REVIEW_INPUT_KEYS` input hashes, and the hash of the bound Stage 6 artifact. Record it in every Stage 7 dispatch record and Stage 7 evidence record.
- R02-FR2: Each distinct review-input identity receives `stage_attempt_budget` Stage 7 attempts once. Attempts used per identity shall be persisted before dispatch and reconstructible from durable dispatch records after a crash. Resume, recovery, and repeated invalidation of the same identity shall not replenish it.
- R02-FR3: Remove the unconditional `force=True` reset for Stage 7. A new identity starts its own count; an exhausted identity blocks with a reason naming the identity and the existing `approve-retry` route. The existing approval semantics are unchanged.
- R02-FR4: Status and report shall show the current review-input identity and attempts used/allowed for it. Earlier attempts remain in history and usage records.

**Constraints.** Other stages keep their current budget behavior. Do not introduce task-wide budgets (V3 V10). The Stage 4 gate loop's `force` handling is out of scope.

**Acceptance.** With budget 1, a failing re-review of one identity dispatches exactly once, no matter how many times `run` is repeated. A later source or review-config change grants exactly one more attempt. A crash after dispatch does not refund the attempt.

## Slice R03 — Durable failed-writer approval

**Problem description / scope.** D3 and D7. `failed_writer_requires_approval` (`agent_pipeline/controller.py:1933`) sets `pending_approval` in memory only. A controller crash before the final `write_state_atomic` loses it, and the resumed run launches a second writer over the partially changed source without approval. Separately, `capture_writer_source_baseline`/`changed_files_since` (`agent_pipeline/manifest.py:16`, `:29`) hash content only, so a failed writer that only changes the mode of an already-dirty file is not detected. Scope: failed write-capable attempts and their recovery. Traces to C03-FR4 and C08-FR3/FR4.

**Functional requirements**

- R03-FR1: When a failed writer requires approval, persist `pending_approval` and the blocked state with `write_state_atomic` before returning or doing anything else. If that write fails, stop with a durable-storage error; launch nothing.
- R03-FR2: Recovery shall find any write-capable attempt whose completion record is not eligible for promotion and that has no recorded approval or resolution. It shall compare the dispatch's implementation baseline with the current source. If changes are found, or the comparison is uncertain, recovery recreates the source-bound approval requirement and blocks instead of dispatching another writer.
- R03-FR3: Writer source comparison shall detect content, executable-bit/mode, deletion, rename, index, and HEAD changes, including a second change to an already-dirty file. Using the C06 source identity alongside the existing baseline is acceptable. The pre-dispatch baseline in the dispatch record shall capture whatever the comparison needs.
- R03-FR4: Recording the approval requirement shall not reset the original implementation baseline or the Stage 5 attempt count.

**Constraints.** Never reset or revert user files. Existing approvals bound to unchanged source keep working.

**Acceptance.** Inject a crash immediately after a failed writer changes a file and before approval bookkeeping. The resumed run blocks at `awaiting_retry_approval` without launching a writer. A mode-only change to an already-dirty file after a failed writer requires approval. An unchanged failed writer still retries within budget.

## Slice R04 — Interruption stops the invocation

**Problem description / scope.** D4. `run_to_files` (`agent_pipeline/real_runner.py:235`) reaps the child on SIGINT/SIGTERM but then returns an exit code instead of stopping. `run_driven_project_checks` (`agent_pipeline/verification.py:232`) goes on to launch the remaining checks, and `run_stage6_transition` (`agent_pipeline/controller.py:841`) then calls the overseer agent, a paid call, after the operator pressed Ctrl-C. Scope: interruption propagation through agent invocations, verification checks, and the run/verify commands. Traces to C05-FR1/FR2.

**Functional requirements**

- R04-FR1: After `run_to_files` terminates and reaps an interrupted child, the interruption shall propagate to its caller as a distinct interruption outcome, not as an ordinary failed exit code.
- R04-FR2: Once interrupted, the same controller invocation shall launch no further agent, check, overseer, or setup process. Remaining verification checks are recorded as not attempted.
- R04-FR3: Before releasing ownership, persist the interrupted outcome: the attempt or verification record marked `interrupted` (superseding older passes under C06-FR5), and the task state not presented as success. `catenna run` and `catenna verify` exit with `EXIT_INTERRUPTED` (130).
- R04-FR4: Timeout handling is unchanged: a timed-out child is a failure of that attempt, not an interruption of the whole invocation.

**Constraints.** Keep the five-second graceful termination and the existing ownership records. Do not add a `cancel` command (V3 V07).

**Acceptance.** A real subprocess test sends SIGINT and SIGTERM during the first of two configured checks: the second check never starts, no overseer or agent is invoked afterwards, the verification record is `interrupted`, the command exits 130, and no managed process is left alive. The same holds for SIGINT during an agent stage.

## Slice R05 — Configured checks bind manual acceptance

**Problem description / scope.** D5 and operator decision 1. `evidence.stage06_status` (`agent_pipeline/evidence.py:185`) evaluates the manual route without looking at verification. A manual `Accept` therefore stays current after a newer configured check fails. Scope: Stage 6 eligibility, evaluation, and the acceptance boundary. Traces to amended C06-FR2.

**Functional requirements**

- R05-FR1: When the effective configuration has at least one `verification.driven_project_commands` entry, manual Stage 6 evidence is current only if `verification_status` is current and passed for the current source and inputs.
- R05-FR2: This applies when manual notes are bound (`ensure_current_stage6`), when status/dry-run/report evaluate current acceptance (`evidence.evaluate`), and at the final acceptance boundary (`ensure_current_decision`).
- R05-FR3: A manual `Reject` or `Needs follow-up` never requires passing checks.
- R05-FR4: With no driven-project checks configured, manual-only acceptance behaves as it does today.
- R05-FR5: The blocking reason names the failing, interrupted, missing, or unbound check and the corrective action: fix the source and re-run `catenna verify`, or record a non-accepting decision.

**Constraints.** Do not change the automatic route's eligibility rules or the checkbox-only decision parsing from C02.

**Acceptance.** Manual `Accept` with a failed configured check is not current in status and cannot reach Stage 7. After a passing `catenna verify` on unchanged source, the same notes proceed. A newer failed `verify` revokes current acceptance. Manual `Reject` proceeds to a `reject` decision despite failing checks. With no checks configured, manual acceptance works unchanged.

## Slice R06 — Protect execution-ownership records

**Problem description / scope.** D6. `invocation_runtime_paths` (`agent_pipeline/controller.py:1502`) excludes the task `lock.json` from integrity checks, and `capture_protected_integrity` (`:1520`) never covers the worktree `execution-lock.json`. An agent can delete either lock undetected, which lets a second writer take the worktree mid-run. Both files change legitimately during an invocation (managed-child records), so hashing them is not enough. Scope: the C07 postcondition. Traces to amended C07-FR4 and C04.

**Functional requirements**

- R06-FR1: After every agent invocation and verification check, confirm that the task lock and the worktree execution lock both still exist, parse, and name this host, run ID, and controller PID.
- R06-FR2: A missing, unreadable, or replaced ownership record fails the postcondition. Promotion is blocked, the reason names the record, and the controller does not re-create the lock silently or continue dispatching.
- R06-FR3: Controller-authorized updates such as managed-child records remain permitted.

**Constraints.** Detection only; no new sandbox. Keep the existing lock order and unlock rules.

**Acceptance.** Deleting or rewriting either lock during a fake agent run blocks promotion with a reason naming the lock. Normal runs, including ones that record managed children, still pass.

## Slice R07 — Snapshot consistency and actionable stale reasons

**Problem description / scope.** D8 and operator decision 3. `capture_source_identity` (`agent_pipeline/source_identity.py:42`) re-checks only HEAD and the index after hashing. A file edited mid-capture can produce an identity that matches neither the old nor the new content. Separately, stale reasons such as "source identity changed during verification" do not say which files changed. That makes check-created untracked files (for example `__pycache__/`) hard to diagnose. Scope: identity capture and stale-evidence reasons. Traces to C06-FR3/FR5 and the C06 constraints.

**Functional requirements**

- R07-FR1: A capture shall detect content changes to hashed entries during the capture (for example, by comparing `lstat` size, mtime_ns, ctime_ns, inode, and mode before and after reading each entry, or by an equivalent consistency check). It may retry up to three times, then raises `SourceIdentityError`.
- R07-FR2: Verification, Stage 6, Stage 7, and Stage 8 stale reasons shall include the changed paths from `changed_identity_paths` when both identities are complete. List at most 20 paths, then give the remaining count.
- R07-FR3: When a changed path is untracked, the reason shall add a hint: output created by a check must be ignored by Git (for example, `__pycache__/`), or verification will never be current.

**Constraints.** No configurable identity exclusions and no automatic `.gitignore` edits (decision 3). The fingerprint components and schema stay stable; existing recorded identities remain comparable.

**Acceptance.** A file mutated while it is being hashed produces a retry or `SourceIdentityError`, never an identity accepted as valid. A check that writes an untracked `__pycache__` file yields a stale reason naming that path with the ignore hint.

## Slice R08 — Remediation gate

**Problem description / scope.** The gate must close only when D1–D8 have permanent, cross-slice regressions and all C and R requirements have passing evidence. Scope: the gate record, gate tests, and operator documentation.

**Functional requirements**

- R08-FR1: Convert the review probes (listed in the gate record) into permanent tests with inverted expectations, one or more per defect D1–D8, in a new module such as `agent_pipeline/tests/test_r_cleanup_remediation.py`.
- R08-FR2: Extend `test_c09_cleanup_gate.py` so the requirement set covers R01–R08 and `gate_is_closed` requires every C and R row to be `PASS`.
- R08-FR3: Update `docs/v3prep/v2-cleanup-gate.md`: tested revision and date, integration results, a `PASS` row per R requirement, each reopened C row back to `PASS` with its new evidence, and a defect table showing each D closed.
- R08-FR4: Update `docs/USAGE.md` to cover the manual route with configured checks, per-identity re-review allowance, interruption exit behavior, and the ignore-rule guidance for check output.

**Constraints.** The gate stays open while any required test is skipped or failing. No paid providers.

**Acceptance.** Both required suites pass with only the optional packaging smoke skipped. Every D1–D8 has a passing regression, and the gate record shows `closed`. Only then may V3 milestone 1 begin.
