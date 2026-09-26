# Catenna usage guide

Operator guide for running Catenna against a driven project. For architecture
details, see [OVERVIEW.md](OVERVIEW.md). Commands can be invoked either as
`catenna ...` after installation or as `python3 -m agent_pipeline.cli ...`;
both forms run the same CLI.

## Quickstart

From this repository, install Catenna:

```bash
pip install -e .
```

or:

```bash
pip install .
```

In the driven project's repository, initialize editable Catenna state:

```bash
catenna init
```

`catenna init` creates these paths when needed:

- `.agent-pipeline/tasks/`
- `.agent-pipeline/usage/`
- `.agent-pipeline/config/orchestrator.json`

If `.agent-pipeline/config/orchestrator.json` already exists, `catenna init`
leaves it unchanged. Use `catenna init --force` to overwrite that config with
defaults from `agent_pipeline/config.py::DEFAULT_CONFIG`.

Edit `.agent-pipeline/config/orchestrator.json` for the driven project,
especially `verification.driven_project_commands` if Stage 6 auto-verification
should ever qualify. Select a task:

```bash
catenna use my-task
```

Before a real run, seed the first two artifacts by hand or with overseer
input:

- `.agent-pipeline/tasks/my-task/00_original_request.md`
- `.agent-pipeline/tasks/my-task/01_requirements_packet.md`

Real `catenna run` validates both seed artifacts before calling agents. It
blocks if either file is missing or invalid.

```bash
catenna run --background
catenna tail
```

Running in the background from the start (rather than foreground first,
backgrounding later) frees the shell immediately; `catenna tail` follows
progress from the same terminal. The selected current task is used when a
task-taking command omits the positional `task`. You can also pass it
explicitly:

```bash
catenna run my-task --background
python3 -m agent_pipeline.cli run my-task --background
```

## Task Lifecycle

All task artifacts live under `.agent-pipeline/tasks/<task>/`.

| Stage | Artifact | Written by |
|-------|----------|------------|
| `00` | `00_original_request.md` | Human or overseer seed |
| `01` | `01_requirements_packet.md` | Human or overseer seed |
| `02` | `02_technical_spec.md` | Real read-only agent |
| `03` | `03_audit.md` | Real read-only agent |
| `04` | `04_final_codex_brief.md` | Real read-only agent |
| `04_gate` | `04_final_brief_audit.md` | Real read-only independent gate agent |
| `05` | `05_codex_implementation_report.md` | Real workspace-write agent |
| `06` | `06_manual_test_notes.md` | Human or overseer manual notes, or controller-written auto-verification notes when automatic Stage 6 verification qualifies |
| `07` | `07_diff_review.md` | Real read-only independent diff-review agent |
| `08` | `08_decision.md` | Deterministic controller synthesis; no agent call |

Stage 8 can synthesize `accept`, `reject`, or `needs_followup`. A task can
reach state `complete` with a non-`accept` decision; in that case `run`
returns validation failure even though all numbered artifacts exist.

## Operating Commands

Task-taking commands use an optional positional `task`. If omitted, Catenna
uses `.agent-pipeline/current-task`, set by `catenna use <task>`.

Common supervision commands:

```bash
catenna status [task]
catenna dry-run [task]
catenna brief [task] --verbose
catenna report [task]
catenna tail [task]
```

Run and verification can be launched in the background:

```bash
catenna run [task] --background
catenna run [task] --bg
catenna verify [task] --background
catenna verify [task] --bg
```

The background parent exit code only means the child process launched. Use
`catenna tail`, `catenna status`, `catenna report`, and the files under the
task's `.orchestrator/` directory to inspect progress.

`usage` is different from task-taking commands: `--task` is a filter and it
does not consult the current-task pointer.

```bash
catenna usage
catenna usage --task my-task
catenna usage --agent codex --since-hours 24
```

Other useful commands:

```bash
catenna tasks
catenna tasks --plain
catenna approve-retry [task] --approval-id <id>
catenna unlock [task] --reason <reason>
catenna verify [task]
catenna verify [task] --build
```

`verify --build` also runs `./gradlew build` when `gradlew` exists, in
addition to the normal verification set.

## State Troubleshooting

The controller state is one of `failures.VALID_STATES`:

| State | Meaning | Operator action |
|-------|---------|-----------------|
| `ready` | No active run; next work can be resumed. | Use `catenna dry-run` to see the next stage, then `catenna run`. |
| `running` | A run is or was in progress. | Use `catenna tail`, `catenna status`, and `.orchestrator/runs/*.stdout`; check for an active lock before intervening. |
| `awaiting_retry_approval` | A bounded expensive retry needs approval. The controller asks only while an attempt remains for the stage's current input identity. | Inspect `catenna status` or `catenna report`, then run `catenna approve-retry --approval-id <id>` if the retry is intentional. |
| `awaiting_human_test` | Stage 6 needs manual notes. | Perform task-specific testing, write `06_manual_test_notes.md`, then run `catenna run` again. |
| `awaiting_final_decision` | Defined state, but not a clear normal real-driver recovery path in current source. | Inspect `status` and `report`, reconcile artifacts, and avoid inventing a recovery step. |
| `blocked` | The controller stopped on a condition requiring human action. | Read `last_failure` in `status`, inspect artifacts and transcripts, fix the cause, then resume with `run` when appropriate. |
| `failed` | Defined state, but not a clear normal real-driver recovery path in current source. | Inspect `status`, `report`, and artifacts before changing anything; recover based on the concrete failure. |
| `complete` | All stages are valid on disk. | Read `08_decision.md`; `complete` is not the same as accepted. Treat `reject` and `needs_followup` as real feedback. |

`status`, `dry-run`, and `run` reconcile artifacts with durable attempt and
evidence records. A structurally valid report alone is not proof that its
process succeeded, and uncertain execution is blocked rather than adopted.

## Cleanup safety guarantees

### Explicit decisions

Stage 6 accepts exactly one checked option in its authoritative `## Decision`
section: `Accept`, `Reject`, or `Needs follow-up`. Narrative wording never
authorizes progression. Missing, duplicate, or conflicting sections/options
remain unresolved. Legacy prose-only notes are readable, but an operator must
add one explicit checkbox choice before Stage 7 can run. Stage 8 applies
`reject` over `needs_followup` over `accept` when combining current Stage 6
and Stage 7 evidence.

Automatic Stage 6 acceptance is controller-owned. It requires enabled
automation, a current passing verification report, at least one configured
driven-project check with every configured check passing, an unflagged
coverage signal, and no blocked or administrator-action handoff. Agent output
claiming `auto_verified` is invalid evidence and cannot grant acceptance.

### Evidence freshness

Verification records the source identity before and after checks. The identity
covers the Git base/HEAD, index, tracked working files and modes, deletions,
and non-ignored untracked files; it also binds the consumed brief, gate, and
effective verification/review configuration. `status` exposes the identity
that manual notes must cite.

Any applicable source, task-input, or configuration drift makes Stage 6–8
evidence historical rather than current. Historical artifacts are retained
for inspection, but `run` cannot return acceptance until new verification or
manual evidence and review are bound to the current identity. A later failed,
interrupted, malformed, or otherwise unverified attempt supersedes an older
pass for the same inputs. Legacy evidence with no identity is historical and
unverified; it is never upgraded by inference.

### Exclusive execution

`run`, `verify`, and their background forms acquire exclusive ownership for
the canonical Git worktree as well as the task lock. A competing run or
verification in the same physical worktree launches no child and reports the
holder's task, run, host, and process. Symlink and subdirectory aliases resolve
to the same owner. Separate physical worktrees may proceed independently, and
read-only supervision commands such as `status` and `report` remain available
while execution owns the worktree.

`unlock` will not remove live or uncertain task/worktree ownership. Inspect
the reported owner and child record first; only a positively stale owner can
be archived by explicit unlock.

### Interruption and recovery

Agent and check children run as managed process groups. SIGINT, SIGTERM, and
timeouts request group termination, allow at most five seconds for graceful
exit, then force termination and reap the direct child before releasing
execution ownership. Partial output and a non-success result are retained.

Before dispatch, the controller durably records the attempt, inputs, retry or
approval consumption, implementation baseline, and process identity. On
resume it may adopt a uniquely identified, successfully completed attempt and
finish promotion once. Incomplete, conflicting, unowned, storage-failed, live,
or uncertain execution remains blocked. Recovery does not replenish attempts
or approvals, infer success from report text, reset source, or automatically
retry an uncertain writer. A failed writer that changed—or may have changed—
source requires explicit approval bound to that attempt and current source
before another writer can start. When no attempt remains for its input
identity, no approval is requested (see [Stage attempt allowance](#stage-attempt-allowance)).

### Manual acceptance with configured checks

When `verification.driven_project_commands` lists at least one check, those
checks bind both Stage 6 routes. A manual `Accept` is current only while the
newest verification for the current source and inputs passed. This is checked
when the notes are bound, whenever `status`, `dry-run`, or `report` evaluate
current acceptance, and again at the final Stage 8 boundary. A newer failed,
interrupted, missing, or unbound check therefore revokes a manual acceptance
that was previously current. The task waits at `awaiting_human_test` with a
reason that names the configured check(s). To recover, fix the source and
re-run `catenna verify`; once a passing verification is bound to unchanged
source, the same notes proceed. Alternatively, record `Reject` or
`Needs follow-up`, which never require passing checks. With no configured
checks, manual-only acceptance works as before.

### Stage attempt allowance

Stages `02`, `03`, `04`, `04_gate`, and `05` count attempts per consumed-input
identity: a digest of the stage key and the hashes of the task artifacts the
stage consumed at dispatch. Each distinct identity gets `stage_attempt_budget`
attempts once. The count comes from durable dispatch records, so repeated
`run`, resume, crash recovery, or invalidation never refills it, and a crash
after dispatch does not refund the attempt. Changing a consumed input (for
example, editing `02_*` before Stage 03) produces a new identity with its own
allowance; reverting to an earlier, exhausted identity does not. Completion
retries and approved max-turn retries keep their existing semantics.

When an identity is exhausted, `run` blocks and names the stage, the identity,
and the attempts used. While blocked on a budget, `status` prints
`stage_attempts: <stage> used/allowed (stage input identity <id>)` and
`report` shows the same.

Exhaustion never creates or consumes an approval, and an approval never refills
a budget. If a failed writer changed source on the last attempt of its
identity, the block names the changed (or uncertain) paths, says that further
writers are blocked, and says no retry approval was created. Your source files
are kept as they are; a new allowance requires a change to the stage's consumed
inputs, and that new identity still requires source-bound approval before any
writer runs. A pending approval whose stage has no attempt left is marked
withdrawn and kept in history; `approve-retry` on it reports the withdrawal
instead of granting a retry. Use `approve-retry` only when `status` shows a
pending approval ID.

### Re-review allowance

Stage 7 attempts are counted per review-input identity: a digest of the
current source identity, the consumed brief/gate/Stage 5 report/review
configuration, and the bound Stage 6 notes. Each distinct identity gets
`stage_attempt_budget` review attempts once. The count is recorded durably
before dispatch, so repeated `run`, crash recovery, or invalidating the same
identity again never refills it, and a crash after dispatch does not refund
the attempt. When an identity is exhausted, `run` blocks and names the identity.
A source, input, or review-configuration change produces a new identity with
its own allowance. An exhausted review identity creates no approval, and
`approve-retry` cannot refill it; the block names the identity and says a new
allowance requires a source, review-input, review-config, or bound Stage 6
change.
`status` prints `review_input_identity` and `review_attempts: used/allowed`,
and `report` shows the same; earlier attempts stay in history and usage.

### Interruption exit behavior

SIGINT or SIGTERM during `catenna run` or `catenna verify` stops the whole
invocation, not only the current child. After the managed child is terminated
and reaped, the controller launches no further agent, check, overseer, or
setup process. Remaining verification checks are recorded as `not_attempted`,
and the interrupted check or agent attempt is recorded as `interrupted`. That
record supersedes any older pass. Under `run`, the task is left `blocked`
with a `process_interrupted` failure rather than presented as success. Both
commands exit with status 130. A timeout is different: it fails only that attempt or check, and the remaining
checks still run.

### Ignoring check output

Source identity is strict: it includes every non-ignored untracked file, and
there is no configurable exclusion list. A check that writes files into the
worktree (for example Python's `__pycache__/`, coverage data, or build output)
therefore changes the source identity during verification. That verification
then never becomes current. The stale reason lists the changed paths (at most
20, then a remaining count). When any of them is untracked, it adds a hint
that check output must be ignored by Git. Add such output to the project's
`.gitignore` (or `.git/info/exclude`) yourself, for example `__pycache__/`,
then re-run `catenna verify`. Catenna never edits ignore rules for you.

### Legacy-task limitations

Legacy task artifacts remain readable, but old prose-only decisions require a
checkbox and old Stage 6–8 evidence without source/provenance identities is not
current acceptance. The controller preserves historical files instead of
rewriting them. The legacy Makefile workflow has no equivalent source-bound,
durable automatic-acceptance guarantee; do not treat its completion as a
current Python-orchestrator acceptance or migrate a task by copying only its
final reports.

## Config Reference

The default config is `agent_pipeline/config.py::DEFAULT_CONFIG`, loaded from
`.agent-pipeline/config/orchestrator.json` and validated by
`config.load_config` / `config.validate_config`.

| Field | Default | Confirmed consumer |
|-------|---------|--------------------|
| `schema_version` | `2` | `config.validate_config` |
| `default_safety_mode` | `"strict"` | `controller.choose_real_agent` |
| `supported_safety_modes` | `["strict", "continuity"]` | `config.validate_config` |
| `stage_attempt_budget` | `2` attempts per consumed-input identity | `controller.ensure_real_stage` |
| `max_gate_passes` | `2` | `gates.run_stage4_gate_loop` |
| `timeout_seconds` | `3600` | `real_runner.invoke_agent` |
| `roles` | Stage and overseer role map | `config.configured_candidates`, `controller.choose_real_agent`, `controller.run_overseer_or_fallback`, `gates.run_stage4_gate_loop` |
| `roles.<stage>.primary` | Agent name | `config.configured_candidates` |
| `roles.<stage>.fallbacks` | Agent list | `config.configured_candidates` |
| `roles.<stage>.independent_from` | Present on `04_gate` and `07` | `controller.choose_real_agent` |
| `roles.<stage>.model_override` | Optional, not in defaults | `controller.merge_stage_override_into_config`, `real_runner.invoke_agent` |
| `roles.<stage>.effort_override` | Optional, not in defaults | `controller.merge_stage_override_into_config`, `real_runner.invoke_agent` |
| `enable_auto_verified` | `true` | `controller.run_overseer_or_fallback` |
| `usage_ledger.enabled` | `true` | `controller.usage_ledger_enabled`, `controller.pipeline_usage`, `controller.invoke_stage` |
| `pricing.codex` | `{}` | `real_runner.invoke_agent`, `usage.estimate_cost_usd` |
| `cost_control.enabled` | `false` | `controller.run_real_pipeline`, `controller.merge_matching_stage_override_into_config`, `cost_policy.compute_stage_overrides` |
| `cost_control.quality_aware` | `false` | `controller.run_real_pipeline`, `gates.record_stage4_quality_outcome`, `cost_policy` |
| `cost_control.min_samples` | `5` | `cost_policy` |
| `cost_control.max_retry_rate` | `0.2` | `cost_policy` |
| `cost_control.max_rejection_rate` | `0.2` | `cost_policy` |
| `cost_control.eligible_stages` | `["02", "03", "04", "04_gate", "07"]` | `cost_policy`, `config.validate_cost_control_config` |
| `cost_control.downgrade_candidates` | `{"claude": {"model": "claude-haiku-4-5", "effort": "low"}, "codex": null, "agy": null}` | `cost_policy` |
| `cross_task_cooldowns.enabled` | `true` | `controller.load_cross_task_cooldowns`, `controller.record_cross_task_cooldown` |
| `cross_task_cooldowns.default_cooldown_seconds` | `900` | `controller.record_cross_task_cooldown`, `usage.record_cooldown` |
| `reasoning_capture.enabled` | `true` | `controller.invoke_stage`, `real_runner.invoke_agent` |
| `agents.codex.command` | `"codex"` | `real_runner.build_argv` |
| `agents.codex.model` | `null` | `real_runner.build_argv`, `real_runner.invoke_agent`, `controller.pipeline_usage` warning |
| `agents.codex.read_args` | `[]` | `real_runner.build_argv` |
| `agents.codex.write_args` | `[]` | `real_runner.build_argv` |
| `agents.codex.overseer_args` | `[]` | Present in defaults; no confirmed current consumer in targeted source inspection |
| `agents.codex.workspace_write` | `true` | `controller.choose_real_agent` |
| `agents.codex.enabled` | `true` | `controller.choose_real_agent`, `controller.run_overseer_or_fallback` |
| `agents.claude.command` | `"claude"` | `real_runner.build_argv` |
| `agents.claude.model` | `null` | `real_runner.build_argv` |
| `agents.claude.read_effort` | `"medium"` | `real_runner.build_argv` |
| `agents.claude.write_effort` | `"medium"` | `real_runner.build_argv` |
| `agents.claude.read_args` | `[]` | `real_runner.build_argv` |
| `agents.claude.write_args` | `[]` | `real_runner.build_argv` |
| `agents.claude.workspace_write` | `false` | `controller.choose_real_agent` |
| `agents.claude.enabled` | `true` | `controller.choose_real_agent`, `controller.run_overseer_or_fallback` |
| `agents.agy.command` | `"agy"` | `real_runner.build_argv`, `real_runner.detect_agy_prompt_mode` |
| `agents.agy.model` | `null` | Present in agent detail metadata; no CLI model flag in `real_runner.build_argv` for agy |
| `agents.agy.common_args` | `[]` | `real_runner.build_argv` |
| `agents.agy.read_args` | `["--mode", "plan"]` | `real_runner.build_argv` |
| `agents.agy.write_args` | `["--mode", "accept-edits"]` | `real_runner.build_argv` |
| `agents.agy.prompt_mode` | `"auto"` | `real_runner.detect_agy_prompt_mode` |
| `agents.agy.stdin_mode_allowed` | `false` | `real_runner.build_argv` |
| `agents.agy.workspace_write` | `false` | `controller.choose_real_agent`, `real_runner.build_argv` |
| `agents.agy.enabled` | `true` | `controller.choose_real_agent`, `controller.run_overseer_or_fallback` |
| `turn_budgets` | `{"02": 20, "03": 20, "04": 20, "04_gate": 20, "05": 40, "07": 20, "overseer": 10}` | `real_runner.build_argv`, `real_runner.invoke_agent` |
| `allow_degraded_same_agent_review` | `false` | `controller.choose_real_agent` |
| `verification.driven_project_commands` | `[]` | `controller.pipeline_verify`, `controller.run_real_pipeline`, `verification.run_verification` |
| `verification.skip_self_check` | `false` | `controller.pipeline_verify`, `controller.run_real_pipeline`, `verification.run_verification` |
| `verification.build_implies_compile` | `false` | `controller.pipeline_verify`, `controller.run_real_pipeline`, `verification.run_verification` |

Nested schemas validated by `config.py`:

- `pricing.codex.<model>.input_tokens`: required non-negative number.
- `pricing.codex.<model>.output_tokens`: required non-negative number.
- `pricing.codex.<model>.cache_read_tokens`: required non-negative number.
- `pricing.codex.<model>.cache_creation_tokens`: required non-negative number.
- `verification.driven_project_commands[].name`: required non-empty string matching `^[A-Za-z0-9_.-]+$`, unique across commands.
- `verification.driven_project_commands[].argv`: required non-empty list of strings.
- `verification.driven_project_commands[].timeout_seconds`: optional positive integer; defaults to the verification module's driven-project timeout when omitted.

Codex cost accounting is local estimate-only: `codex exec --json` token usage
is priced from `pricing.codex` and `agents.codex.model`, and no provider-real
Codex cost field is recorded without a confirmed JSONL schema. `pricing.codex`
defaults to `{}`, so cost estimation can stay unavailable unless rates and
`agents.codex.model` are configured. Cost control may have no effect until
enough usage and outcome samples exist for its thresholds.
`verification.driven_project_commands` defaults to `[]`; with no driven
project checks configured, `driven_project_verified` is false and Stage 6
auto-verification cannot qualify.

## Self-Hosting Overseer Workflow

For changes to Catenna itself:

1. Confirm the tree is clean with `git status --short`.
2. Branch from `main`.
3. Run `catenna use <task>`.
4. Write `.agent-pipeline/tasks/<task>/00_original_request.md` and `01_requirements_packet.md` by hand.
5. Run `catenna run --background` (or `--bg`) for the whole task, from the
   first invocation — don't run it in the foreground and switch over later.
   This frees the shell immediately instead of blocking it for however long
   Stage 02-05's real agent calls take.
6. Use `catenna tail` (and `catenna status`) from the same or another
   terminal to monitor progress instead of watching a blocked foreground
   process.
7. Supervise with `catenna status`, `catenna dry-run`, `catenna brief --verbose`, and `catenna report`.
8. When complete, read `08_decision.md`; treat `reject` and `needs_followup` as real feedback, not as success.
9. Run `python3 -m unittest discover -s agent_pipeline/tests` as an explicit gate.
10. Perform task-specific manual verification.
11. Commit intentionally, without a `Co-Authored-By` trailer.
12. Push the branch and open a PR against `main`.

## Troubleshooting Quick Reference

- Dirty tree before Stage 05: the current controller message is `Source working tree is not clean outside .agent-pipeline; rerun with --allow-dirty if intentional`.
- Stale lock: inspect `catenna status` and `catenna report` context first, then use `catenna unlock <task> --reason <reason>` only when the lock is known stale.
- Raw agent transcripts live under `.agent-pipeline/tasks/<task>/.orchestrator/runs/*.stdout`.
- Verification run stdout lives under `.agent-pipeline/tasks/<task>/.orchestrator/verification_runs/*.stdout`.
- Summaries are available through `catenna brief --verbose` and `catenna report`.
- Verification output includes `.agent-pipeline/tasks/<task>/05_verification_report.md` and `.agent-pipeline/tasks/<task>/05_verification_report.json`.
