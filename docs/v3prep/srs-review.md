# V3 preparation — Executive summary and requirement report

Reviewed: 2026-09-22. Normative specifications: [V2 cleanup](v2-cleanup.md) and [V3](v3.md). This is a specification review, not an implementation-completion report or a new reproduction of runtime defects. All requirements remain subject to their implementation gates. Table checks below are required evidence, not passing test results.

## Executive summary

The product direction is a local developer CLI that delivers an isolated, reviewable patch with evidence tied to the actual source, while letting an operator inspect, redirect, and recover execution. The original review addressed failure boundaries and handoffs; the approved planning expansion adds operator control and measured efficiency without requiring constant supervision.

Cleanup must close the eight [confirmed high-priority audit findings](../audits/2026-09-21.md#priority-fixes) before V3 starts. Its requirements now also prevent unapproved retries over a failed writer's changes, reuse of superseded evidence, unauthorized control-record changes, and false success after storage failures. These are extensions to the safety requirements, not newly confirmed audit findings.

Milestones 1–3 retain the execution foundation, complete local workflow, and measured provider/profile/CI capabilities. Milestone 4 adds intervention across `02`, `03`, `04`, `04_gate`, `05`, `07`, and agent-backed overseer calls, dynamic stage model/effort selection, and multi-task supervision. Milestone 5 adds run history, offline pipeline diagnostics, recovery previews, safe reuse, bounded context, and repeated-failure prevention. Existing IDs are preserved; V13–V21 are the added slices. Hosted services, new providers, automatic publishing, and parallel agents writing one task worktree remain excluded.

Efficiency means less wasted work per accepted task, not simply a cheaper model or fewer tokens in one call. Operator choices remain explicit; continued generation still consumes budget. Native steering is capability-dependent, acknowledgment is not semantic compliance, and immutable provenance/review rules still govern acceptance. Cleanup remains a prerequisite, not work deferred into later diagnostics.

| Section | Slices | Developer outcome | Required exit gate |
| --- | --- | --- | --- |
| V2 cleanup | C01–C09, R01–R08 | Decisions and recovery cannot silently authorize unsafe or stale work. | All A1–A8 and D1–D8 regressions, cleanup requirement evidence, and integration suites pass (gate closed 2026-09-25); follow-up slices F01–F05 must also close their [follow-up gate](v2-cleanup-followups-gate.md) against a commit. |
| V3 milestone 1 | V00–V02 | Installable CLI with explicit lifecycle, storage, compatibility, and resource contracts. | Clean installed-package checks and foundation requirement evidence pass. |
| V3 milestone 2 | V03–V08 | Create, prepare, run, verify, correct, and export a task without editing internal state. | End-to-end developer examples and all local-workflow requirement checks pass. |
| V3 milestone 3 | V09–V12 | Effective provider controls, bounded execution, measured profile tradeoffs, and trustworthy CI consumption. | Adapter/budget/profile/CI checks pass and the comparative evaluation is recorded. |
| V3 milestone 4 | V13–V16 | Steer, hold, redirect, configure, and supervise every active agent stage across isolated tasks. | All-stage control/recovery fixtures, capability evidence, and multi-task demonstration pass. |
| V3 milestone 5 | V17–V21 | Explain pipeline faults, preview recovery, reuse valid work, and measure avoided repetition. | Offline diagnostics, reuse/failure-suppression checks, and comparative efficiency evidence pass. |

Delivery order is cleanup → milestone 1 → milestone 2 → milestone 3 → milestone 4 → milestone 5. A skipped mandatory check leaves its gate open; documentation changes alone close none of these gates.

## Missed boundaries addressed

| Gap | Why it matters | Requirement home |
| --- | --- | --- |
| Failed process versus changed source | Rejecting output does not undo an implementation's edits; an automatic retry can compound them. | C03-FR4 |
| Evidence precedence and control integrity | An old passing report or modified policy must not become current authority. | C06-FR5; C07-FR4 |
| Storage failure and interrupted migration | Atomic-looking files alone do not establish durable, compatible state. | C08-FR5; V01-FR4 |
| Operational and non-functional contracts | Launch success, acceptance, resource exhaustion, and private-data handling need distinct behavior. | V00; V02-FR4 |
| Contract authority and enforceable scope | Markdown and structured criteria can disagree; out-of-scope edits need explicit resolution. | V04-FR4–FR5 |
| Usable, reviewable isolated workspaces | A new worktree lacks dependencies, and a clean status can hide committed changes from review. | V05-FR4–FR6 |
| Verification validity | Zero tests, changed dependencies, and later failures cannot silently reuse earlier passes. | V06-FR5 |
| Follow-up ancestry and complete export | Starting a correction from HEAD can lose the parent patch; exporting only the correction is incomplete. | V07-FR4; V08-FR2–FR4 |
| Settings, budgets, and profile compatibility | Unknown provider events, crashed counters, and omitted stages must not bypass the controller. | V09-FR4; V10-FR4–FR5; V11-FR4–FR5 |
| Evidence authenticity | A self-consistent bundle can still be fabricated by an untrusted producer. | V12-FR4 |
| Persistent sessions versus process completion | A successful turn can finish while its transport remains alive; interrupted output still cannot authorize a stage. | C03-FR1; V13-FR2–FR3 |
| Late or duplicated operator input | A correction must reach the intended attempt, survive recovery, and not leak into the next stage. | V14-FR1–FR2 |
| Guidance versus authority | Steering cannot silently amend scope, bypass a gate, or guarantee a tool stops. | V14-FR3–FR5 |
| Dynamic settings versus budget/history | A pinned choice needs a clear activation boundary; session reuse must not hide continued spending. | V15 |
| Observation versus usable supervision | Operators need targeted actions and attention signals without mandatory babysitting or shared-worktree races. | V16 |
| Pipeline debugging versus another paid run | Recorded events should explain/reproduce controller faults without rerunning the agent or mutating evidence. | V17–V18 |
| Reuse and history versus stale authority | Useful history must not become unrelated prompt bulk or transferable acceptance. | V19 |
| Cheaper calls versus cheaper accepted work | Known faults should not repeat blindly; quality, failures, unknown usage, and operator attention belong in the comparison. | V20–V21 |

## V2 cleanup — Requirement report

Source: [cleanup SRS](v2-cleanup.md). Audit mapping: C01=A1, C02=A2, C03=A8, C04=A6, C05=A5, C06=A3, C07=A4, C08=A7; C09 is the combined release gate. Within each slice, its problem/scope and constraints apply to every row.

| Requirement | Required behavior | Acceptance evidence |
| --- | --- | --- |
| C01-FR1 | Agent output cannot grant automatic acceptance. | Claimed `auto_verified` cannot bypass controller eligibility. |
| C01-FR2 | Only the controller checks all automatic-transition prerequisites. | Disabled automation, failed/missing checks, flagged coverage, or blocked handoff prevent advancement. |
| C01-FR3 | Invalid evidence stays non-automatic; historical controller handoffs remain readable. | Malformed and persisted-handoff compatibility cases. |
| C02-FR1 | Exactly one explicit manual decision controls Stage 6. | Each checkbox choice works; negative prose cannot approve. |
| C02-FR2 | Missing or conflicting decisions remain unresolved. | Duplicate sections/options and absent-choice cases. |
| C02-FR3 | Legacy prose needs explicit approval; stricter valid decisions win. | Legacy-note and reject/follow-up/accept precedence cases. |
| C03-FR1 | One-shot promotion requires successful process exit, valid output, and execution guards. | Valid artifact plus failed execution cannot advance; V13 specifies the later session equivalent. |
| C03-FR2 | Nonzero exit, interruption, timeout, and terminal errors block promotion. | Each failure class tested, including terminal error with exit zero. |
| C03-FR3 | A permitted retry earns independent authority and retains failed evidence. | Separate attempt identities and immutable failure records. |
| C03-FR4 | A failed writer's changed/uncertain source requires durable, bound retry approval. | Completion/fallback/crash-resume cannot silently run; mode-only changes count; original baseline and budget survive. |
| C04-FR1 | Worktree and task ownership cover all source-dependent execution. | Cross-task write/write and write/check subprocess contention. |
| C04-FR2 | Same-worktree competitors launch nothing; different worktrees remain independent. | Locked outcome includes holder; distinct worktrees can proceed. |
| C04-FR3 | Atomic, consistently ordered ownership cannot discard a live/uncertain holder. | Alias, acquisition-race, lock-order, and unsafe-unlock tests. |
| C05-FR1 | Cancel the process group, allow at most five seconds, then kill/reap before unlock; launch nothing further. | SIGINT, SIGTERM, and timeout leave no live managed group; no later check or agent starts. |
| C05-FR2 | Persist child identity before waiting and retain interrupted output. | Launch/interruption records include host, run, group, and process-start identity. |
| C05-FR3 | Orphaned or uncertain children block reuse. | Kill controller; recovery cannot start competing work or kill an unrelated process. |
| C06-FR1 | Fingerprint complete relevant source and consumed policy/artifacts. | Commit/index/content/mode/delete/untracked/symlink changes are represented. |
| C06-FR2 | Checks, manual evidence, review, and decisions bind to current source; configured checks bind both Stage 6 routes. | Mid-check drift, mismatched identities, and manual `Accept` over a failed configured check fail eligibility. |
| C06-FR3 | Relevant changes invalidate downstream evidence without rewriting source; re-reviews get one budget per review-input identity. | Drift requires renewed evidence naming changed paths; resumes cannot refill a re-review budget. |
| C06-FR4 | Legacy or stale evidence is not current acceptance. | Status/report distinguish history; `run` cannot accept missing identities. |
| C06-FR5 | Incomplete snapshots and newer failed attempts defeat an older pass. | Unreadable/racing snapshots and pass-then-fail reconciliation cases. |
| C07-FR1 | Every read-only invocation uses the same content-based postcondition. | Normal, completion, and overseer edits to already-dirty files are detected. |
| C07-FR2 | Mutation or comparison failure blocks promotion and downstream dispatch. | Changed paths/failure reason are retained; no subsequent automatic stage starts. |
| C07-FR3 | Adapters cannot silently substitute writable permissions. | Unsupported read-only mode blocks before launch. |
| C07-FR4 | All attempts protect approved inputs, policy, controller records, and ownership records. | Tampering, including lock deletion, cannot authorize results. |
| C08-FR1 | Persist dispatch identity, inputs, consumed authorization, and baseline first. | Failed pre-dispatch persistence launches no child. |
| C08-FR2 | Promotion is recoverable and attempt artifacts are uniquely identified. | Crash points around completion/promotion; same-second attempts cannot overwrite evidence. |
| C08-FR3 | Adopt proven success once; block ambiguous execution; never refresh inputs or restore invalidated results. | Recovery finishes bookkeeping without repeating implementation or resurrecting stale acceptance. |
| C08-FR4 | Recovery retains budgets/approvals and checks process ownership. | Restart cannot replenish attempts or treat valid prose as successful execution. |
| C08-FR5 | Durable writes fail safely under disk/permission errors. | Inject write/sync failures; retain valid state, stop active children, never publish success. |
| C09-FR1 | Every A1–A8 finding has a permanent regression. | Real subprocess and temporary-Git tests exercise the actual guards. |
| C09-FR2 | Full unittest and mock suites pass with intentional compatibility changes. | Recorded suite results; old defect-demonstration scripts are not counted as fixes. |
| C09-FR3 | Operator documentation and tested-revision evidence match corrected behavior. | Inspect decision, freshness, ownership, recovery, and legacy guidance. |
| C09-FR4 | Every cleanup requirement has traceable proof. | Requirement-to-test/inspection record has no unresolved mandatory entry. |

## V3 milestone 1 — Execution foundation

Source: [milestone 1 SRS](v3.md#milestone-1--execution-foundation). Depends on C09. V00 supplies the shared contracts used by every later slice.

| Requirement | Required behavior | Acceptance evidence |
| --- | --- | --- |
| V00-FR1 | Separate lifecycle, stage progress, decision, and freshness; track background runs by ID. | Repeated runs and failed preflight cannot reuse old completion reports. |
| V00-FR2 | Stable exit categories distinguish operation success from acceptance; snapshots and explicit event streams have separate formats. | Command/outcome matrix, single-result JSON, and V16 JSON Lines stdout tests. |
| V00-FR3 | Live status/reporting needs no provider call or execution ownership. | Supervision remains available during locked execution without state mutation. |
| V00-NFR1 | Bound output capture and parsed-event size; cancel on overflow. | Noisy/oversized-event fixtures hit configured limits with incomplete outcomes. |
| V00-NFR2 | Private runtime permissions, credential-free metadata, explicit retention/capture defaults. | Permission/default-configuration checks and sensitive-field fixtures. |
| V01-FR1 | Resolve one project/task/workspace context from root, subdirectory, or explicit path. | Equivalent invocations select identical records and worktree. |
| V01-FR2 | Validate configuration and persisted-record schemas before dispatch. | Invalid nested types, references, arguments, and limits fail without provider calls. |
| V01-FR3 | Load supported legacy records without inventing provenance. | Legacy fixtures retain diagnostics and remain unverified where necessary. |
| V01-FR4 | Versioned, recoverable migrations preserve previous records. | Interrupted migration and newer-schema/older-binary tests. |
| V01-FR5 | Enforce managed path boundaries and tested Git-feature support. | Traversal/symlink cases, supported tree features, and explicit unsupported-state failures. |
| V02-FR1 | Build/install the actual distribution outside the source checkout. | Fresh wheel environment runs console and module entry points. |
| V02-FR2 | Credential-free CI covers minimum/development Python and required installed fixtures. | Linux matrix runs unittest, mock, and cleanup regressions. |
| V02-FR3 | Publish supported installation/release procedure and ignore generated build data. | Clean-install walkthrough; existing artifacts remain untouched. |
| V02-FR4 | Each milestone has revision/environment-specific requirement evidence. | No skipped mandatory check or untested advertised capability closes a gate. |

## V3 milestone 2 — Complete local developer workflow

Source: [milestone 2 SRS](v3.md#milestone-2--complete-local-developer-workflow). Depends on milestone 1. V04 defines task authority; V05 supplies the source/environment identity used by V06–V08. Report fields unavailable until milestone 3 explicitly remain unknown.

| Requirement | Required behavior | Acceptance evidence |
| --- | --- | --- |
| V03-FR1 | Idempotent initialization preserves configuration and protects private runtime data. | Repeat init in existing/new repositories without overwritten user ignore rules. |
| V03-FR2 | Doctor reports actionable pass/fail/unknown prerequisite results. | Missing CLI, invalid config, workspace, command, and reviewer cases. |
| V03-FR3 | Provide a credential-free first task and final report. | Follow the documented fake-agent example without hand-building state. |
| V03-FR4 | Enforce preflight and independent final-review eligibility on execution. | `run` cannot bypass a failed prerequisite or stale preflight inputs. |
| V04-FR1 | Task creation preserves the request and creates a versioned acceptance contract. | Draft contains objective, scope, context, and uniquely identified criteria. |
| V04-FR2 | Every criterion maps to checks/manual evidence before dispatch. | Incomplete drafts, nonexistent check IDs, and invalid context paths block. |
| V04-FR3 | Contract changes invalidate evidence; legacy documents stay accessible. | Revised criteria cannot reuse incompatible acceptance. |
| V04-FR4 | Structured contract is authoritative; divergent Markdown requires reconciliation. | Edit either representation; no silent precedence or lost criterion history. |
| V04-FR5 | Enforce allowed change paths and exclusions at acceptance. | Out-of-scope changes require explicit contract amendment and renewed evidence. |
| V05-FR1 | Create/reuse a dedicated task worktree with execution/delivery bases. | Two tasks have distinct source trees; resume finds the recorded workspace. |
| V05-FR2 | Route planning, implementation, checks, and review to that worktree. | Caller-directory changes cannot redirect task execution. |
| V05-FR3 | Preserve the developer's dirty checkout and pin legacy workspaces. | No implicit copy/stash/reset; unsupported setup blocks before dispatch. |
| V05-FR4 | Explicit, bounded setup records dependencies/runtime inputs and outcomes. | Fresh-worktree preparation succeeds or blocks clearly; input drift invalidates reuse. |
| V05-FR5 | Separate authoritative control storage and recover interrupted workspace creation. | Candidate-output access does not grant control-record authority; unrelated paths are not adopted. |
| V05-FR6 | Review the complete immutable change from the delivery base. | Committed, dirty, binary, and untracked changes appear in the reviewed snapshot. |
| V06-FR1 | Use explicit project-check presets, not unrelated Catenna/Gradle defaults. | Python/JavaScript examples invoke only configured project checks. |
| V06-FR2 | Capture check identity, process outcome, raw output, and known structured details. | Pass/fail/launch-error/timeout fixtures retain usable evidence. |
| V06-FR3 | Record source-bound manual criteria; all required evidence must pass on either decision route. | Manual Stage 6 approval cannot waive failed or missing criterion evidence. |
| V06-FR4 | Separate passed, failed, and unverified criterion outcomes. | Unmapped checks and heuristic coverage cannot satisfy criteria. |
| V06-FR5 | Bind verification to task/environment inputs and reject empty/malformed test evidence. | Zero/all-skipped tests, dependency changes, and pass-then-fail cases stay unverified/failed. |
| V07-FR1 | Cancel only the recorded owned run and preserve work/evidence. | Active, inactive, unrelated-process, and uncertain-ownership cases. |
| V07-FR2 | Expose the next action and consume bounded retry authorization. | Approval/resume works without state editing or counter reset. |
| V07-FR3 | Create a bounded follow-up draft without changing the parent decision. | Parent findings/criteria are linked; no overwrite or automatic run. |
| V07-FR4 | Follow up from exact parent source while retaining the original delivery base. | Dirty-parent changes survive in the child; missing/drifted parent source blocks; evidence is re-earned. |
| V08-FR1 | Human/JSON reports expose identities, evidence, findings, usage completeness, and next action. | Stable schema distinguishes unknown fields and historical/current decisions. |
| V08-FR2 | Export the complete patch and identities relative to the delivery base. | Apply ordinary and follow-up exports to that base and compare resulting trees. |
| V08-FR3 | Export a consistent snapshot using an explicit metadata allowlist. | Stale/rejected labels are accurate; private raw metadata is excluded. |
| V08-FR4 | Publish complete exports atomically without overwriting outputs. | Mutation, cancellation, disk failure, and existing/symlink targets cannot produce apparent success. |

## V3 milestone 3 — Measured efficiency and integration

Source: [milestone 3 SRS](v3.md#milestone-3--measured-efficiency-and-integration). Depends on milestone 2. Provider observations feed budgets/evaluation; CI consumes V08's format through a trusted producer boundary.

| Requirement | Required behavior | Acceptance evidence |
| --- | --- | --- |
| V09-FR1 | Declare adapter capabilities; reject unsupported explicit settings. | Capability/preflight fixture matrix. |
| V09-FR2 | Apply provider-specific overrides and record requested/effective settings. | Fallback never receives another provider's model setting. |
| V09-FR3 | Forward supported controls correctly and enforce independent final review. | Captured argv plus missing/incompatible-reviewer blocking cases. |
| V09-FR4 | Test CLI/event versions and prevent unsafe provider argument overrides. | Unknown required events do not pass; permission/limit/output-routing overrides fail. |
| V10-FR1 | Enforce task-wide call/time limits across all retries and resumes. | Exhaustion stops dispatch or cancels active work; human wait is excluded. |
| V10-FR2 | Distinguish enforceable provider limits from estimates/thresholds. | Unsupported hard-cap claims fail; possible overshoot remains visible. |
| V10-FR3 | Report normalized usage, cost provenance, missing observations, and remaining budget. | Partial ledgers and provider cache fixtures cannot produce falsely complete totals. |
| V10-FR4 | Require finite limits and durably reserve allowance before dispatch. | Crash around launch does not refund uncertain calls or erase elapsed work. |
| V10-FR5 | Amend budgets explicitly under inactive-task ownership. | Consumption/history persist; lower-than-consumed limits and implicit retry approval are rejected. |
| V11-FR1 | Retain thorough and add only an opt-in compact workflow. | Compact executes its required brief, implementation, evidence, review, and decision stages. |
| V11-FR2 | Record applicable stages and preserve all safety/acceptance guarantees. | Omitted stages are not applicable, never synthetic passes; both profiles pass invariant tests. |
| V11-FR3 | Evaluate at least twelve versioned Python/JavaScript tasks with quality/time/usage evidence. | Recorded live comparison includes independently checked correctness and human review time. |
| V11-FR4 | Version profile graphs and define replacement evidence for omitted gates. | Compact resumes without fabricated thorough artifacts or weakened controller verification. |
| V11-FR5 | Fix comparison conditions and include failures/unknowns. | Published rubric, settings, all-attempt totals, and explicit sample limitations. |
| V12-FR1 | CI rejects incomplete, stale, rejected, or unverified required evidence. | Gate fixture matrix distinguishes launch from final success. |
| V12-FR2 | Match evidence base/resulting tree to the revision under review. | Same tree after commit succeeds; changed base/content fails. |
| V12-FR3 | Separate optional live credentials from credential-free evidence consumption. | Local CI example requires no provider authentication. |
| V12-FR4 | Require trusted evidence origin, not only matching hashes. | Forged self-consistent contributor bundle fails the gate. |

## V3 milestone 4 — Operator control and supervision

Source: [milestone 4 SRS](v3.md#milestone-4--operator-control-and-supervision). Depends on milestone 3. V13 provides session/capability boundaries; V14 supplies durable controls; V15 adds explicit stage settings; V16 exposes them across independent task worktrees. All six agent stages, their retry/fallback paths, and agent-backed overseer calls are in scope; omitted profile stages stay not applicable.

| Requirement | Required behavior | Acceptance evidence |
| --- | --- | --- |
| V13-FR1 | Advertise verified steering, interruption, continuation, and setting-change capabilities for every adapter. | All-stage capability matrix; unsupported steering never silently restarts a process. |
| V13-FR2 | Separate session/turn from run/attempt identity and require confirmed successful attempt completion. | Acknowledgments, live transports, interrupted turns, and valid-looking failed output cannot authorize promotion. |
| V13-FR3 | Isolate task/role conversations and retain process/tool ownership through recovery. | Independent review cannot inherit implementation's private session; uncertain owned work blocks unsafe reuse. |
| V13-FR4 | Keep bounded bidirectional control responsive while output streams. | Oversized/pending requests, acknowledgment timeout, and disconnect produce explicit dispositions without resetting limits. |
| V14-FR1 | Persist exactly targeted, revision-checked control requests through an owner-only channel. | Wrong/stale task, run, stage, or attempt is rejected; execution locks do not prevent control submission. |
| V14-FR2 | Record ordered delivery dispositions and prevent duplicate or late-message misrouting. | Crash/acknowledgment/completion races cannot silently replay input or advance with unresolved accepted requests. |
| V14-FR3 | Separate steering from confirmed interruption and consented continuation/replacement. | Interrupted writers preserve source/baseline and require bound retry approval; fresh invocation is never hidden. |
| V14-FR4 | Hold downstream dispatch deterministically; resume/dismiss preserve guards and history. | A hold before Stage 5 dispatch prevents implementation independently of agent compliance; no budget reset. |
| V14-FR5 | Keep guidance subordinate to explicit scope revisions and stage authority. | Revised briefs are gated again; interventions enter provenance and downstream review without granting acceptance. |
| V15-FR1 | Preview valid task-stage selections with explicit precedence and pinning. | Cost policy cannot override a pin; incompatible fallback and non-independent reviewer selections fail. |
| V15-FR2 | Apply revision-checked pre-run/live settings at the next boundary or through explicit redirection. | Pre-run selection needs no invented run; live activation is explicit, and stale/unsupported changes fail. |
| V15-FR3 | Record effective settings per attempt/continuation without rewriting completed history. | A future selection cannot silently rerun accepted work or relabel previous results. |
| V15-FR4 | Charge new turns/replacements and continued work against retained task budgets. | Keeping one process alive cannot evade call/time accounting; active tool work is not human-wait time. |
| V16-FR1 | Show current task activity, settings, budget, attention needs, and intervention status. | Concurrent-task fixtures expose current versus stale/unknown state and evidence links. |
| V16-FR2 | Present fully targeted actions; keep watch/read-only JSON views non-mutating. | Stale displayed targets cannot control new attempts; reading status launches no provider. |
| V16-FR3 | Supervise isolated tasks without changing within-task ordering or writer ownership. | Correcting/detaching from one task neither redirects nor stops another. |
| V16-FR4 | Provide reconnectable attention events and a credential-free operator workflow. | Demo steers, holds, changes a selection, and resumes without editing state; gaps are explicit. |

## V3 milestone 5 — Run diagnostics and measured efficiency

Source: [milestone 5 SRS](v3.md#milestone-5--run-diagnostics-and-measured-efficiency). Depends on milestone 4. V17 supplies historical evidence; V18 consumes it without live execution; V19/V20 prevent unnecessary repetition; V21 measures results using V11's fixtures and V10 accounting.

| Requirement | Required behavior | Acceptance evidence |
| --- | --- | --- |
| V17-FR1 | Inspect immutable attempts, inputs, identities, settings, versions, interventions, and usage. | Retry/resume history remains navigable; missing legacy data is explicitly unavailable. |
| V17-FR2 | Explain controller decisions through rule/event references and distinguish fault classes. | Provider, artifact, controller, environment, and project-check failures are distinguishable without invented causes. |
| V17-FR3 | Compare attempts' changed inputs, settings, outputs, and consumption. | Differences are observed facts, not unsupported claims that a model change caused success. |
| V17-FR4 | Add compatible history/intervention references and next actions to reports. | Inspection is provider-free/read-only; raw private data requires explicit access. |
| V18-FR1 | Replay captured parsing/validation/controller logic offline in isolated scratch storage. | No provider, project command, hook, source mutation, or live transition occurs. |
| V18-FR2 | Disclose replay/capture version compatibility and missing inputs. | New-engine comparison and partial captures cannot masquerade as exact historical reproduction. |
| V18-FR3 | Preview reuse, invalidation, outstanding controls, approvals, settings, and budget consequences. | Guarded execution rejects a changed recovery plan; preview creates no approval or state mutation. |
| V18-FR4 | Export atomic, allowlisted diagnostic evidence with explicit omissions. | Private raw data is excluded by default; reduced bundles do not claim full reproduction. |
| V19-FR1 | Reuse only evidence whose complete applicable inputs remain current. | Unchanged resume avoids calls; changed/unknown/superseded inputs cannot inherit a pass. |
| V19-FR2 | Retain inspectable, hashed stage context with criteria, decisions, corrections, risks, and impact references. | Operators can inspect dispatched context and distinguish known affected systems from unknown impact. |
| V19-FR3 | Bound controller-prepared context without silently dropping mandatory evidence. | Optional history is reduced first; oversized required context blocks with a corrective action. |
| V19-FR4 | Use selected historical context as referenced background, never transferred authority. | Cross-task reports cannot supply acceptance or implicit scope; summaries remain non-authoritative. |
| V20-FR1 | Fingerprint and classify faults using component/version and relevant input identities. | Similar error wording alone does not establish deterministic equivalence. |
| V20-FR2 | Suppress unchanged deterministic faults and bound transient/unknown retries. | Known prerequisite/controller faults cause no further paid dispatch until corrected and revalidated. |
| V20-FR3 | Record explicit retry reasons/changes without resetting authorization or budgets. | Manual actions cannot waive mandatory guards or hide the previous failed attempt. |
| V20-FR4 | Aggregate recurring faults and observed rework with evidence links. | Controller failures remain distinct from application failures and model-quality judgments. |
| V21-FR1 | Attribute usage/time/attention honestly across attempts and continuations. | Cumulative observations are deduplicated; unknown attention and estimated avoided work remain labeled. |
| V21-FR2 | Compare fixed efficiency variants with predeclared operator and evaluation conditions. | Autonomous and assisted results are separate; combined changes disclose attribution limits. |
| V21-FR3 | Include all failed/incomplete work when reporting correctness and cost per accepted task. | Zero accepted tasks and missing observations yield undefined/incomplete metrics, not misleading savings. |
| V21-FR4 | Require dispatch-avoidance proofs and measured comparisons before efficiency claims. | Reuse/suppression fixtures avoid calls, safety regressions pass, and benchmark limitations remain visible. |

## Assumptions and remaining release decisions

| Item | Boundary or decision | Closure point |
| --- | --- | --- |
| Supported environment | Linux, declared Python minimum (currently 3.8), supported local filesystems, trusted local operator/repository. Compatibility is required, not yet demonstrated by this review. | V01/V02 tested support matrix. |
| Resource defaults | Five-second termination grace, 64 MiB output, and 1 MiB parsed-event limits are specified starting limits. Task call/time, control-queue/payload/acknowledgment, and context-input limits need documented finite defaults and measurement methods. | C05/V00 tests; V10/V13/V19 documented limits. |
| Git/provider compatibility | Exact supported feature/CLI-version combinations must be published from test evidence, not assumed universal. | V01 and V09 release evidence. |
| Product benefit | Twelve evaluation tasks and added recovery scenarios provide bounded comparisons, not universal quality/cost guarantees. Assisted outcomes include operator attention and intervention costs; no savings percentage is promised. | V11/V21 recorded evaluations. |
| Trust and retention | Worktrees are not security sandboxes; raw outputs/source may contain secrets. Retention is manual and local bundles are not self-authenticating. | V00/V05/V08/V12 operator guidance and boundary tests. |
| Live control support | Exact steering/interruption/model-change behavior is adapter/version-specific. At least one existing provider must demonstrate native steering; unsupported modes remain visible rather than silently degrading. | V13 capability evidence and opt-in budgeted smoke check. |
| Diagnostic fidelity | Historical captures may lack inputs or compatible controller semantics. Offline replay is a parser/controller comparison, not a replay of model reasoning or a new live attempt. | V17/V18 capture-version and isolation tests. |

These decisions do not defer any A1–A8 fix. Their owning slice cannot close without its specified evidence.
