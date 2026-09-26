# V2 cleanup — Software Requirements Specification

Status: C01–C09 implemented; **gate reopened 2026-09-25** for cross-slice defects. Remediation slices R01–R08 are in [v2-cleanup-remediation.md](v2-cleanup-remediation.md); see the [cleanup completion gate](v2-cleanup-gate.md).
Source: [repository audit, findings A1–A8](../audits/2026-09-21.md#priority-fixes).

Purpose: resolve all eight high-priority findings before any [V3 milestone](v3.md) begins. Deliver slices C01–C09 in order, then remediation slices R01–R08. Requirement IDs are stable identifiers for implementation tasks and regression tests.

Companion: [executive summary and requirement report](srs-review.md). Requirements here are normative; that report summarizes them.

Scope excludes new worktree management, onboarding commands, provider expansion, workflow profiles, and the audit's unrelated P2 improvements. Preserve existing CLI forms, stage filenames, review-independence rules, and retry budgets except where a requirement below explicitly changes unsafe behavior.

## Slice C01 — Controller-owned automatic acceptance

**Problem description / scope.** A1: an overseer can return `auto_verified` and bypass verification. Scope: overseer parsing and the Stage 6 automatic transition.

**Functional requirements**

- C01-FR1: Agent proposals shall accept only `manual_test`, `blocked`, or `administrator_action`. An agent-provided `auto_verified` shall be rejected as authority; any controller fallback shall independently satisfy C01-FR2 before completing Stage 6.
- C01-FR2: Only the controller shall authorize automatic Stage 6 completion. At the transition, it shall require automatic verification enabled, a controller-produced passing verification report, at least one configured driven-project check with all configured checks passed, no flagged coverage signal, and no blocked/administrator-action handoff.
- C01-FR3: Missing, malformed, or ineligible evidence shall retain a non-automatic checkpoint and an explicit reason. Persisted controller-generated `auto_verified` handoffs shall remain readable without granting that authority to new agent output.

**Constraints.** Retain the existing manual-testing route and coverage policy. When C06 lands, automatic eligibility shall also require its current-evidence checks. Do not add an LLM decision step.

**Acceptance.** A claimed automatic route never advances Stage 6 with disabled automation, absent/failed checks, or flagged coverage. Eligible controller evidence still advances normally.

## Slice C02 — Explicit manual decisions

**Problem description / scope.** A2: positive keywords inside negative prose become acceptance. Scope: Stage 6 decision validation and Stage 8 consumption.

**Functional requirements**

- C02-FR1: Stage 6 shall require exactly one checked `Accept`, `Reject`, or `Needs follow-up` box in its authoritative decision section. Narrative text shall not determine the result.
- C02-FR2: Missing, duplicated, or conflicting decision sections/checked options shall leave Stage 6 unresolved and identify the required correction.
- C02-FR3: Legacy prose-only notes shall remain readable but shall require an explicit checkbox decision before authorizing continuation. Stage 8 shall retain `reject > needs_followup > accept` precedence over valid Stage 6 and Stage 7 results.

**Constraints.** Do not rewrite historical notes or infer approval from their wording. Preserve existing decision labels and Markdown artifact paths.

**Acceptance.** `Not approved`, `Tests did not pass`, and `Do not accept this change` cannot authorize acceptance; each valid checkbox choice produces the corresponding result.

## Slice C03 — Successful execution before artifact promotion

**Problem description / scope.** A8: structurally valid output can override a failed process. Scope: normal attempts, completion retries, reviews, and overseer results.

**Functional requirements**

- C03-FR1: For the existing one-shot adapters, an agent result shall become authoritative only after a completed process exits successfully, no recognized terminal failure is present, and its output passes the stage contract and applicable execution guards.
- C03-FR2: Nonzero exits, timeouts, interruptions, and recognized provider error events shall prevent promotion, including when a complete-looking artifact exists. A recognized terminal error shall not be ignored solely because the process exits zero.
- C03-FR3: Failed output may supply context to an already permitted retry. Only the independently successful retry result may be promoted, with its own attempt identity; failed evidence shall remain available.
- C03-FR4: After a failed write-capable attempt, compare source against its pre-dispatch baseline. Changed or uncertain source shall block another writer, including completion/fallback attempts, until explicit retry approval bound to that attempt and current source is recorded. Approval shall not reset the original implementation baseline or attempt budget. The approval requirement shall be durable before the controller returns or dispatches anything else, and a controller crash shall not remove it. Source comparison shall detect content, file-mode, deletion, index, and HEAD changes, including changes to files that were already dirty.

**Constraints.** Preserve existing retry limits and approval requirements. Apply one promotion policy across all invocation paths; do not introduce automatic output salvage as success. Persistent sessions are excluded from cleanup; [V3 slice V13](v3.md#slice-v13--controllable-agent-sessions) defines their later equivalent terminal-attempt boundary.

**Acceptance.** A failed Stage 7 result containing `accept` cannot authorize Stage 8; successful normal/completion results still progress. A failed writer that changed source cannot silently trigger another writer or lose its original baseline.

## Slice C04 — Exclusive ownership of a shared worktree

**Problem description / scope.** A6: different task locks permit concurrent execution in one source tree. Scope: existing `run`, `verify`, background equivalents, and unlock behavior.

**Functional requirements**

- C04-FR1: Before dispatch or verification, Catenna shall acquire exclusive ownership keyed to the canonical Git worktree, in addition to its task lock. Ownership shall cover the invocation through its last source-dependent operation.
- C04-FR2: Competing runs/verifications for that worktree shall launch no child and return the existing locked outcome with holder/task information. Different physical worktrees may execute independently.
- C04-FR3: Ownership acquisition shall be atomic, use a consistent lock order, and identify the host/run/process. Live or uncertain ownership shall not be removed by unlock or recovery.

**Constraints.** Serialize the existing shared-worktree workflow; do not create new worktrees in this slice. Read-only supervision commands remain available without acquiring execution ownership. Cancellation signaling shall not wait for the execution lock held by its target. Revalidate evidence on later resume after ownership is released.

**Acceptance.** Separate processes competing in write/write and write/verify scenarios cannot both start. Symlink/subdirectory aliases resolve to the same ownership key. Status remains usable during a run.

## Slice C05 — Cancellation and orphan ownership

**Problem description / scope.** A5: interruption releases the task lock while the child continues. Scope: managed agent/check subprocesses and recovery after controller death.

**Functional requirements**

- C05-FR1: SIGINT, SIGTERM, and timeout handling shall terminate the managed process group, allow at most five seconds for graceful exit, then force termination and reap the direct child before releasing execution ownership. After an interruption, the same controller invocation shall launch no further agent, check, or setup process.
- C05-FR2: Before waiting on a launched child, Catenna shall persist its run, process/group, host, and process-start identity. Interruptions shall persist a non-success outcome and retain partial output.
- C05-FR3: Following abrupt controller death, recovery shall check recorded child ownership before permitting another execution. A live or uncertain child shall keep the worktree blocked; only positively identified owned processes may be terminated.

**Constraints.** Use the existing POSIX execution boundary. SIGKILL cannot be handled by the killed process; recovery must handle its retained records. An incomplete/uncertain launch record shall block reuse. Do not add a new cancellation CLI in cleanup.

**Acceptance.** SIGINT/SIGTERM/timeout leave no running member of the managed group after successful cleanup. A killed controller cannot be unlocked into a competing run while its child remains live or unverified.

## Slice C06 — Verification and review bound to source identity

**Problem description / scope.** A3: source changes retain an obsolete acceptance. Scope: source identity, Stage 6–8 evidence, reconciliation, and status/report interpretation.

**Functional requirements**

- C06-FR1: Catenna shall fingerprint the base/HEAD commit, index, tracked working-file contents and modes, deletions, and non-ignored untracked files. Tracked symlink targets shall be represented without following links outside the worktree. Record accepted-brief/gate hashes and effective verification/review configuration with the evidence.
- C06-FR2: Verification shall record the source identity before and after checks and qualify as current only when they agree. Manual notes shall explicitly cite the identity exposed by status/dry-run; reviews and final decisions shall retain that binding. Continuation and acceptance shall revalidate those identities. When driven-project checks are configured, acceptance through either Stage 6 route requires the newest verification for the current source and inputs to have passed; a manual `Accept` cannot waive a failed, interrupted, unbound, or missing configured check. Manual-only acceptance remains available when no driven-project checks are configured. Recording `Reject` or `Needs follow-up` does not require passing checks.
- C06-FR3: Changed source or relevant evidence/configuration shall invalidate Stage 6–8 as applicable, preserve historical results, and require new verification/manual evidence and review. Source drift alone shall not rerun implementation or overwrite source files. Stale-evidence reasons shall name the changed paths when they are known. A review required because its inputs changed is a new review: it receives the stage attempt budget once per distinct review-input identity (source identity, review inputs, and bound Stage 6 artifact), recorded durably; resumes, recovery, and repeated invalidation shall not replenish it.
- C06-FR4: Missing identities on legacy evidence shall mean unverified, not current acceptance. Status/report shall distinguish historical decisions from current eligibility; `run` shall not return acceptance using stale evidence.
- C06-FR5: An incomplete, unreadable, or internally inconsistent source snapshot, including one whose file content changed while it was being captured, shall remain unverified. A later failed, interrupted, or unverified required check/review shall supersede older passing evidence for the same inputs; reconciliation shall not select an older pass to restore acceptance.

**Constraints.** Exclude controller-owned `.agent-pipeline/` data from source identity; retain hashes of the explicitly consumed task artifacts separately. Ignored, untracked build output is outside this source identity. Non-ignored untracked files, including output created by checks, remain inside it; cleanup adds no configurable identity exclusions (ignore-rule setup belongs to V3 `init`/`doctor`). Preserve upstream task-artifact invalidation rules.

**Acceptance.** Changes to committed, staged, dirty, deleted, renamed, or untracked source invalidate old acceptance. Changes during verification also invalidate its result. Unchanged source/evidence can resume without repeating implementation.

## Slice C07 — Uniform read-only postconditions

**Problem description / scope.** A4: status-string comparisons miss content changes, and completion/overseer paths omit checks. Scope: every read-only agent invocation.

**Functional requirements**

- C07-FR1: Normal attempts, completion retries, and overseer calls shall compare C06 source identities before and after execution and apply the same mutation policy before accepting output.
- C07-FR2: A mutation or unavailable comparison shall block promotion and further dispatch, invalidate affected downstream evidence, and record the attempt plus changed paths or comparison failure.
- C07-FR3: Each adapter shall continue to request its supported read-only execution controls. A provider unable to satisfy the configured read-only mode shall not be silently dispatched with write permissions.
- C07-FR4: Every agent attempt, including implementation, shall protect approved task inputs, effective policy/configuration, and controller-owned records from unauthorized changes. Detectable changes to these protected inputs/records outside designated candidate-output paths shall block promotion and preserve diagnostic evidence; excluding runtime data from the source fingerprint shall not exempt these records from integrity checks. Task and worktree execution-ownership records shall still exist and identify the current run after every attempt; deletion or replacement blocks promotion.

**Constraints.** Retain changed files for inspection; never automatically reset user work. Controller-authorized record updates and designated agent candidate outputs are permitted. Postconditions detect persistent changes, not every transient edit or hostile same-user process; this is not a new security sandbox.

**Acceptance.** Altering an already-dirty file is detected in normal, completion, and overseer calls. Such output cannot authorize automatic verification or a final decision.

## Slice C08 — Durable dispatch and recovery

**Problem description / scope.** A7: a crash between artifact promotion and state persistence loses required provenance. Scope: durable run records and reconciliation of interrupted transitions.

**Functional requirements**

- C08-FR1: Before dispatch, Catenna shall persist the run/attempt identity, selected agent/mode, input hashes, retry/approval consumption, and implementation baseline. Dispatch shall not proceed if that record cannot be saved.
- C08-FR2: Completion and artifact promotion shall follow a recoverable sequence associating process outcome, candidate/final hashes, and provenance. Attempt inputs/results shall use unique immutable paths; canonical stage artifacts may reference/promote those results.
- C08-FR3: Recovery shall adopt a demonstrably successful completed attempt and finish its missing bookkeeping/postprocessing without another implementation call. An incomplete, conflicting, or unowned execution shall block with a specific recovery reason. Recovery shall not refresh consumed-input hashes from current files, adopt an attempt whose consumed inputs changed after its dispatch, or restore a result that C06 invalidated or a later promoted attempt superseded; such attempts are historical and do not count as conflicts.
- C08-FR4: Recovery shall retain attempt counts and consumed approvals, enforce C05 ownership checks, and never infer successful execution from a valid-looking report alone.
- C08-FR5: Durable transitions shall use atomic replacement and file/directory synchronization where required by the supported local filesystem. Storage-full, permission, or write failures shall retain the last valid record and block success; if a child is active, use managed cancellation while retaining ownership until termination is confirmed.

**Constraints.** Extend existing local state/sidecar storage; no database migration or wholesale controller rewrite. Guarantees cover supported local filesystems, not network/shared filesystems. Read legacy records without fabricating missing provenance. Cleanup shall not automatically retry an uncertain write attempt.

**Acceptance.** Inject crashes before launch, after launch, after completion recording, around artifact promotion, and during postprocessing. Successful recorded work resumes once; uncertain work stays blocked; retries/approvals are not replenished by a crash.

## Slice C09 — Cleanup completion gate

**Problem description / scope.** The eight fixes must operate together before V3 changes the workflow. Scope: regression evidence and documentation of the corrected guarantees.

**Functional requirements**

- C09-FR1: Each finding A1–A8 shall have a permanent regression test demonstrating the corrected behavior. Concurrency/cancellation tests shall use real local subprocesses; source-drift tests shall inspect actual temporary Git working trees.
- C09-FR2: The full unittest suite and `mock-test` shall pass, including updated expectations for intentional decision/provenance compatibility changes. Existing audit demonstrations shall not be treated as passing regression tests because they assert the old defects.
- C09-FR3: Current operator documentation shall describe explicit decisions, stale-evidence handling, exclusive execution, interruption/recovery, and legacy-task limitations. Record the tested revision and results for all C01–C08 acceptance cases.
- C09-FR4: The gate record shall map every cleanup requirement ID to a passing test or explicit inspection result, including failed-write retries, stale-result supersession, control-record tampering, and storage failures. Skipped required checks shall leave the gate open.

**Constraints.** No paid providers are required for this gate. Mock fixtures shall not bypass the guards being tested. Do not report completion while any A1–A8 regression remains unresolved.

**Acceptance.** All eight findings have traceable passing regressions and the integration suite passes. Only then may V3 milestone 1 begin.
