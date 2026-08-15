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
| `awaiting_retry_approval` | A bounded expensive retry needs approval. | Inspect `catenna status` or `catenna report`, then run `catenna approve-retry --approval-id <id>` if the retry is intentional. |
| `awaiting_human_test` | Stage 6 needs manual notes. | Perform task-specific testing, write `06_manual_test_notes.md`, then run `catenna run` again. |
| `awaiting_final_decision` | Defined state, but not a clear normal real-driver recovery path in current source. | Inspect `status` and `report`, reconcile artifacts, and avoid inventing a recovery step. |
| `blocked` | The controller stopped on a condition requiring human action. | Read `last_failure` in `status`, inspect artifacts and transcripts, fix the cause, then resume with `run` when appropriate. |
| `failed` | Defined state, but not a clear normal real-driver recovery path in current source. | Inspect `status`, `report`, and artifacts before changing anything; recover based on the concrete failure. |
| `complete` | All stages are valid on disk. | Read `08_decision.md`; `complete` is not the same as accepted. Treat `reject` and `needs_followup` as real feedback. |

`status`, `dry-run`, and `run` reconcile `.orchestrator/state.json` from the
artifact files, so valid artifacts on disk are the recovery anchor.

## Config Reference

The default config is `agent_pipeline/config.py::DEFAULT_CONFIG`, loaded from
`.agent-pipeline/config/orchestrator.json` and validated by
`config.load_config` / `config.validate_config`.

| Field | Default | Confirmed consumer |
|-------|---------|--------------------|
| `schema_version` | `2` | `config.validate_config` |
| `default_safety_mode` | `"strict"` | `controller.choose_real_agent` |
| `supported_safety_modes` | `["strict", "continuity"]` | `config.validate_config` |
| `stage_attempt_budget` | `2` | `controller.ensure_real_stage` |
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
