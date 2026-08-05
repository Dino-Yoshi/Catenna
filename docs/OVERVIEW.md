# Agent pipeline overview

Living document for `agent_pipeline/` (the Python orchestrator) and how it
relates to the legacy bash pipeline (`Makefile`/`Makefile.legacy`) that still
lives in each driven project's repo. Updated at the end of every phase of the
redesign tracked in [PHASES.md](PHASES.md).

This snapshot reflects the system through the end of Phase 5 of the
redesign.

**Relocation note (2026-08-05):** Phases 0-5 below were built and tested
while this code lived at `tools/agent_pipeline/` (and docs at
`docs/agent-pipeline/`) inside a driven project's own repo
(`immersive-enchanting-1122`), gitignored there the whole time. It has since
been extracted to this standalone repo so it can drive multiple projects.
Path references below that predate this note (`tools/agent_pipeline/...`,
`docs/agent-pipeline/...`) describe that original in-repo layout; the
current layout is `agent_pipeline/...` / `docs/...` here, with each driven
project keeping only `Makefile.orchestrator` (pointed at
`AGENT_PIPELINE_HOME`) plus its own `.agent-pipeline/` task data. See this
repo's own commit history from here forward for what's current; treat
everything below as a historical record of Phases 0-5, not a live snapshot.

## Two pipelines, one task-artifact format

Both pipelines drive coding-agent CLIs (`codex`, `claude`, `agy`/Antigravity)
through the same 8-stage workflow and read/write the same
`.agent-pipeline/tasks/<task>/` artifact files:

| Stage    | Artifact                        | Purpose                              |
|----------|----------------------------------|---------------------------------------|
| 00       | `00_original_request.md`         | Seed: the raw task ask                |
| 01       | `01_requirements_packet.md`      | Requirements/design packet            |
| 02       | `02_technical_spec.md`           | Technical specification               |
| 03       | `03_audit.md`                    | Independent audit of the spec         |
| 04       | `04_final_codex_brief.md`        | Final implementation brief            |
| 04_gate  | `04_final_brief_audit.md`        | Independent audit of the brief        |
| 05       | `05_codex_implementation_report.md` | Implementation report              |
| 06       | `06_manual_test_notes.md`        | Manual test notes                     |
| 07       | `07_diff_review.md`              | Independent diff review               |
| 08       | `08_decision.md`                 | Accept / reject / needs-follow-up     |

`AGENTS.md` treats `04_final_codex_brief.md` as the implementation contract and
`07_diff_review.md`/`08_decision.md` as follow-up context; `status.json` files
can go stale relative to the human-readable stage artifacts, so the artifacts
are the source of truth when they disagree.

## Legacy pipeline: `Makefile` / `Makefile.legacy`

Bash-driven, calls a `claude`/`codex`/`agy` CLI per stage via shell, all 8
stages plus `followup-*` targets. `Makefile.legacy` is the pre-refactor
version of `Makefile` (uses `$(MAKE)` recipe-internal recursion instead of
`$(SELF_MAKE)`) — functionally the same pipeline, kept only as a rollback
reference. **`Makefile.orchestrator` is now canonical for all of Stages
00-08** as of Phase 3 (see [PHASES.md](PHASES.md)); the legacy Makefile
remains available as a fully manual fallback and for `followup-from-review`,
which Phase 3 deliberately left unautomated. Do not delete `Makefile.legacy`
until no task is mid-flight on its stage targets.

Stage 6/7/8 in the legacy pipeline are entirely manual: Stage 6 opens
`$EDITOR` on a blank template, Stage 7 hand-runs one review agent, Stage 8 is
a hand-edited checkbox decision doc, and `followup-from-review` is a bash
template generator that scaffolds a new task directory from a Stage 7 review.
The Python orchestrator's real driver (below) now automates the Stage 6-8
equivalent of this; `followup-from-review` remains legacy-only.

## Python orchestrator: `tools/agent_pipeline/`

A from-scratch, reliability-focused rewrite. Two parallel drivers share the
same state machine:

- **Mock driver** (`controller.run_scenario`/`ensure_stage`, over `MockAgent`)
  — covers all 8 stages deterministically, used by `mock-run`/`mock-test` for
  testing the controller itself without calling real agent CLIs.
- **Real driver** (`controller.run_real_pipeline`/`ensure_real_stage`) — drives
  Stages `00` through `08`. Through Stage 05 unchanged from Phase 2. At Stage
  06 it now calls `verification.run_verification` and, when the overseer's
  handoff route qualifies as `auto_verified` (evidence-gated — see "Stage 6
  auto-verification" below), writes `06_manual_test_notes.md` itself and
  continues straight through Stages 07 (real independent diff-review agent
  call) and 08 (deterministic decision synthesis, no agent call) in the same
  `pipeline-run` invocation. When evidence doesn't qualify, behavior is
  unchanged from Phase 2: it stops at `awaiting_human_test` (Stage 06) for a
  human to write `06_manual_test_notes.md` by hand — but a *subsequent*
  `pipeline-run` now notices that file becoming valid and drives Stages 07/08
  automatically from there, which Phase 2 could not do (see "Known gaps").

### Stage 6 auto-verification (Phase 3)

After Stage 5 and manifest generation, `run_real_pipeline` calls
`verification.run_verification(task_dir, REPO_ROOT, allow_pid=os.getpid())`
(the `allow_pid` bypass lets it call the same concurrency guard
`pipeline-verify` uses without tripping over its own held `TaskLock`) and
passes the resulting report into `run_overseer_or_fallback`. There, the
controller deterministically computes

```
auto_verified_eligible = (
    config.get("enable_auto_verified", True)
    and verification_report is not None
    and verification_report["overall_status"] == "passed"
    and verification_report["test_coverage_delta_signal"]["status"] != "flagged"
)
```

and, if eligible and the handoff's route isn't already `blocked`/
`administrator_action`, calls `overseer.upgrade_to_auto_verified` to force
the route to `auto_verified`. **This decision is never made by the LLM
overseer agent itself** — only by this deterministic check against the real
verification report — so a handoff an agent writes can't talk its way past
the human-testing checkpoint by claiming success. `enable_auto_verified`
(`config.py`, default `true`) is a rollback switch if this proves too
aggressive for a given repo/task mix.

When the route is `auto_verified`, the controller writes
`06_manual_test_notes.md` itself (via the same `runner.atomic_finalize`
primitive the mock driver already uses for its local stages), stating
plainly that no human tested the change in-game and citing the passing
checks. A human can always overwrite this file and flip its checkbox before
Stage 7 runs if they want a real manual pass first.

Stage 08 is synthesized, not agent-generated: `controller.ensure_stage08_decision`
reads Stage 6's outcome (`artifacts.manual_test_decision`, checkbox- or
prose-based) and Stage 7's final verdict line, and combines them with a
"worst wins" rule (`reject` > `needs_followup` > `accept`) into
`08_decision.md`. `pipeline-run`'s process exit code reflects this: `0` for
an overall `accept`, `EXIT_VALIDATION` (`1`) otherwise — the pipeline still
reaches `state: complete` either way (all 8 stage artifacts exist and are
valid), the same way a CI run "completes" whether or not its tests passed.

Key modules:

- `state.py` — state schema (`STAGE_ORDER`, `reconcile_artifacts`,
  `contiguous_completed`, `next_stage`, `invalidated_from` staleness cascade).
- `artifacts.py` — per-stage `ArtifactContract`s (required headings/sections,
  YAML gate blocks, decision-checkbox validation). `manual_test_decision`
  (Phase 3) classifies a Stage 6 (or Stage 8) outcome as
  `accept`/`reject`/`needs_followup` from its checkbox or explicit prose.
- `config.py` — `DEFAULT_CONFIG` (`roles: {stage: {primary, fallbacks,
  independent_from?}}`, `turn_budgets`, per-agent CLI settings, plus
  `enable_auto_verified`), merged with `.agent-pipeline/config/orchestrator.json`
  if present. `roles["07"]` (Phase 3) mirrors the mock driver's long-standing
  `policies.ROLE_POLICY["07"]`.
- `policies.py` / `choose_real_agent` (controller.py) — primary+fallback
  agent selection per stage, with an `independent_from` config field that
  guarantees a reviewer is never the same agent as the implementer it's
  reviewing (used for Stage `04_gate` vs `04`, and — since Phase 3 — Stage
  `07` vs `05`).
- `locking.py` — exclusive per-task `lock.json` (`O_EXCL`), host/PID
  liveness check, explicit unlock archives the stale lock.
- `manifest.py` — pre-Stage-5 dirty-tree baseline, diffed after, to
  attribute changed files to the Stage 5 run.
- `overseer.py` — post-Stage-5 handoff generator: a real agent call
  producing a structured JSON handoff (`route`: `manual_test` / `blocked` /
  `administrator_action` / `auto_verified`), with a deterministic fallback if
  the agent call or JSON parse fails. The agent itself only ever proposes the
  first three routes; `upgrade_to_auto_verified` (Phase 3, called from
  `controller.run_overseer_or_fallback`) is the sole place `auto_verified` is
  ever set, and only from a deterministic check against the real verification
  report — see "Stage 6 auto-verification" above. Both `fallback_handoff` and
  the agent prompt now cite real per-check verification statuses instead of
  the old always-pessimistic placeholder text.
- `real_runner.py` — subprocess adapter per agent CLI, classifies exit
  code/stdout into failure classes (`max_turns`, `usage_limit`,
  `rate_limit`, `timeout`, etc). The invoking process still blocks on
  `process.communicate()`, but stdout is now JSON/stream-json per agent
  (`--json` for codex, `--output-format stream-json` for claude/agy), and
  is written directly to `runs/*.stdout` as it's produced, so a separate
  `pipeline-tail`/`pipeline-brief` process can read live progress from
  another terminal (Phase 1). Also (Phase 5) computes
  `stream_events.reasoning_summary` and, when `capture_reasoning` (opt-in,
  config-gated) and non-`None`, writes `runs/<base>.reasoning.md` and adds
  `result["reasoning_path"]` to the metadata sidecar — same additive-field
  precedent as Phase 4's `result["usage"]`.
- `stream_events.py` — the single place that understands all three CLIs'
  JSONL event schemas: `final_text` (candidate extraction),
  `structured_failure` (additive failure classification, substring
  `classify()` remains the fallback), `summarize_event` (human-readable
  progress lines for `tail.py`), `reasoning_summary` (Phase 5 — best-effort
  chain-of-thought extraction: codex's `reasoning`-typed `item.completed`
  events, claude's `thinking_delta` content-block deltas accumulated per
  block index; agy has no known reasoning-bearing event today, always
  `None`, same "no confirmed fixture" caveat as agy's usage extraction).
- `tail.py` — read-only `locate`/`follow`/`brief` over `runs/*.stdout` +
  `*.json` sidecars; no locking, no state mutation. `brief()` also prints
  `reasoning:`/`reasoning_path:` lines (Phase 5) when the metadata sidecar
  has `reasoning_path` set, mirroring the `usage:` line from Phase 4.
- `failures.py` — exit codes, valid state enum
  (`VALID_STATES` in failures.py), banned-word guard for mock fixtures.
- `runner.py` — atomic candidate-to-final artifact promotion.
- `verification.py` (Phase 2, wired into the automatic flow in Phase 3) —
  build+test evidence gathering. Reachable both standalone/human-triggered
  via `pipeline-verify`, and automatically from inside `run_real_pipeline`
  right after Stage 5's manifest is generated (`allow_pid=os.getpid()` lets
  the latter call bypass `check_concurrency_guard`'s otherwise-correct
  refusal to run while *this same task's* lock is held — see "Stage 6
  auto-verification" above). Three checks —
  `unit_tests` (`python3 -m unittest discover -s tools/agent_pipeline/tests`),
  `mock_pipeline` (`python3 -m tools.agent_pipeline.cli mock-test`),
  `gradle_compileJava` (optionally also `gradle_build` with `--build`/`BUILD=1`)
  — plus a detection-only `test_coverage_delta_signal` that flags Stage 5
  changed files touching testable source (`.py`/`.java`) without a matching
  change under `tools/agent_pipeline/tests/` or `src/test/`. All three
  subprocess checks reuse `real_runner.run_to_files` rather than a fourth
  ad-hoc subprocess pattern. Writes `<task>/05_verification_report.{json,md}`
  and, when `05_implementation_manifest.json` exists for the task, fills in
  that manifest's long-standing `verification: {unit_tests, mock_pipeline,
  diff_check}` placeholders (previously always `"not_attempted"`) plus
  appends to `verification_evidence` — additive, only touches those two
  fields, never `changed_files`/`stage5_run`. See
  [phase-2-handoff.md](handoffs/phase-2-handoff.md) for the full JSON report
  schema.
- `usage.py` (Phase 4) — cross-task, cross-run persistent state under
  `.agent-pipeline/usage/`, independent of `controller`/`TASKS_ROOT` to
  avoid a circular import: `ledger.jsonl` (one line per real agent
  invocation, via `build_entry`/`append_entry`/`read_entries`/`summarize`,
  `fcntl.flock`-guarded appends) and `agent_cooldowns.json` (per-agent
  cross-task cooldowns from `usage_limit`/`rate_limit` failures, via
  `load_cooldowns`/`record_cooldown`, extend-only merge, its own `.lock`
  file plus `tmp+os.replace` for the data file). Every function is
  best-effort — never raises, degrades to `False`/`[]`/`{}`. Wired into
  `real_runner.invoke_agent` (usage extraction via
  `stream_events.usage_summary`, opt-in via a `ledger_path` parameter) and
  `controller.choose_real_agent` (cross-task cooldown reordering, opt-in via
  `cross_task_cooldowns.enabled`). See
  [phase-4-handoff.md](handoffs/phase-4-handoff.md).
- `report.py` (Phase 5) — per-task legible report synthesizing stage
  status/agent/duration/failure, the Stage 8 decision, the Stage 5
  verification report, usage-ledger totals, and captured reasoning traces
  into one document, reachable via `pipeline-report`. Structured like
  `verification.py`: `generate_report(task_dir, task, state,
  usage_entries=None)` returns a dict with `report_paths` after writing
  `<task>/.orchestrator/task_report.{json,md}` (kept under `.orchestrator/`,
  not among the numbered `00`-`08` stage artifacts, since it's derived
  observability output, not a pipeline-contract artifact subject to
  `ArtifactContract` validation); `render_markdown`/`write_report` are
  separately testable. Reads `task_dir` directly for anything task-local
  (state, artifact files, `05_verification_report.json`) and takes ledger
  entries as a parameter rather than importing controller's path constants,
  the same decoupling `verification.py`/`usage.py` already use. Reasoning
  traces are found by globbing `tail.runs_dir(task_dir)` for
  `*.reasoning.md` (see `stream_events.reasoning_summary` below) and paired
  with each run's `.json` metadata sidecar for stage/agent/run_id context.
  See [phase-5-handoff.md](handoffs/phase-5-handoff.md).

### State machine

`state["state"]` is one of: `ready`, `running`, `awaiting_retry_approval`,
`awaiting_human_test`, `awaiting_final_decision`, `blocked`, `failed`,
`complete` (`failures.VALID_STATES`). `reconcile_artifacts` recomputes
`completed_stages`/`current_stage` from what's actually valid on disk on every
`status`/`dry-run`/`pipeline-run` invocation, so resuming after an interruption
is safe by construction.

### Retry/fallback

`ensure_real_stage` retries within a configured attempt budget, classifying
failures (`real_runner.classify`) to decide whether to retry the same agent,
fall back to the next configured candidate, or block for human approval
(`approve-retry`/`unlock` commands). `choose_real_agent` skips any candidate
that would violate an `independent_from` constraint.

## Known gaps (as of Phase 5)

- No live `.agent-pipeline/config/orchestrator.json` exists (only timestamped
  backups: `orchestrator.json.before-claude-stage5-turn-increase-...` and
  `orchestrator.json.profile-era`) — real pipeline runs are silently on
  `DEFAULT_CONFIG`, not any tuned configuration. Not fixed by this redesign;
  flagged here for future attention.
- `followup-from-review` (scaffolding a new correction task from a rejected
  or needs-followup Stage 7 review) remains legacy-bash-only; not automated
  by Phase 3. A human still runs it by hand off of `07_diff_review.md`/
  `08_decision.md`, which Phase 3 populates in the same legacy-compatible
  format/paths regardless of whether they were produced automatically or by
  a human. Deferred, not scheduled to a specific future phase.
- `run_real_pipeline`'s auto_verified path never invokes real in-game/manual
  testing — it is gated on build/unit-test/coverage-signal evidence only,
  which cannot catch GUI/gameplay regressions. `enable_auto_verified: false`
  in `.agent-pipeline/config/orchestrator.json` disables it repo-wide if this
  proves too aggressive; a human can also always overwrite an
  auto-generated `06_manual_test_notes.md` before Stage 7 runs.
- Of the task directories with live `.orchestrator/` state that predate this
  phase, any sitting at `awaiting_human_test` with Stage 6 already completed
  by a human (but Stage 7/8 never manually run) will now have Stage 7/8
  driven automatically on their next `pipeline-run` — previously a permanent
  dead end (see "Real driver" above). Tasks with no Stage 6 file yet are
  unaffected.
- Live visibility granularity varies by agent: claude streams true
  token-level deltas (coalesced by `pipeline-tail` into one line per
  structural transition, e.g. "responding..."); codex/agy stream
  step/turn-level events only, no token-level granularity available from
  their CLIs. Structured failure classification
  (`stream_events.structured_failure`) is additive on top of the existing
  substring-based `classify()`, not a replacement — see
  [phase-1-handoff.md](handoffs/phase-1-handoff.md) for what's verified
  live vs. best-effort/untested against a real failure.
- `pipelineBugs.md` (Stage 6 completion validation, dry-run terseness,
  unittest discovery, decision-checkbox regex, `completed_stages`
  prefix-awareness) documented 8 bugs, **all already fixed** in current code
  as of this Phase 0 pass — the file has been removed; see this changelog
  entry as its historical record.
- Real agent CLIs never report a `reset_at` for `usage_limit`/`rate_limit`
  failures (`real_runner.py`'s result dict has no such field for any real
  invocation, only `mock_agent.py` populates it for mock scenarios) — so
  Phase 4's cross-task cooldown windows are always
  `default_cooldown_seconds`-based in practice, never a genuine CLI-reported
  reset time, and `rate_limit` without a credible reset still hard-blocks a
  task outright. See [phase-4-handoff.md](handoffs/phase-4-handoff.md).
- `usage_summary`'s agy extraction (`stream_events.py`) is unconfirmed
  against a real Antigravity CLI fixture — it will silently return `None`
  for agy unless agy's real `result` event happens to match the speculative
  `usage`/`total_cost_usd`/`cost_usd` field names guessed at in Phase 4.
- `reasoning_summary` (Phase 5) has no known agy event to extract from at
  all (not merely unconfirmed like agy's usage extraction) — agy reasoning
  traces are never captured, repo-wide, until agy's stream-json schema for
  reasoning/thinking content is confirmed against a real fixture.
- Reasoning-trace capture is real-driver-only — the mock driver
  (`MockAgent`) never produces `.stdout` JSONL in any of the three real
  CLIs' schemas, so mock-run tasks never get a `*.reasoning.md` file,
  exactly parallel to how Phase 4's usage ledger only ever gets entries
  from real runs.
- `pipeline-report` is a per-task view only; a cross-task dashboard (task
  statuses, stuck/blocked tasks across `.agent-pipeline/tasks/`, rolled-up
  cost) was explicitly scoped out this phase — see "Deliberately deferred"
  in [phase-5-handoff.md](handoffs/phase-5-handoff.md).
- `pipeline-tail`'s live view still only shows "thinking..." as a status
  line for claude, not the actual reasoning text streaming in — Phase 5
  scoped reasoning visibility to post-hoc (`pipeline-report`/
  `pipeline-brief`'s excerpt), not live-tail.

## Command table

The only interface a user should need is `make -f Makefile.orchestrator
<target> TASK=...` (raw `python3 -m tools.agent_pipeline.cli ...` is the
implementation underneath and is not a documented user-facing path). This
table grows as later phases add commands.

| Target                  | Required vars                | What it does                                   |
|--------------------------|-------------------------------|-------------------------------------------------|
| `help`                   | —                              | Prints CLI help.                                |
| `pipeline-status`        | `TASK`                         | Shows controller status for a task.             |
| `pipeline-dry-run`       | `TASK`                         | Shows resumable work without mutating state.    |
| `pipeline-mock-test`     | —                              | Runs isolated deterministic mock scenarios.     |
| `pipeline-mock-run`      | `TASK`, `SCENARIO`              | Runs one deterministic mock scenario.           |
| `pipeline-run`           | `TASK` (`ALLOW_DIRTY=1` opt.)   | Runs/resumes the real Stage 00-08 pipeline.     |
| `pipeline-resume`        | `TASK`                         | Alias for `pipeline-run`.                       |
| `pipeline-approve-retry` | `TASK`, `APPROVAL_ID`           | Approves one pending expensive retry.           |
| `pipeline-unlock`        | `TASK`, `REASON`                | Explicitly removes an orchestrator lock.        |
| `pipeline-tail`          | `TASK` (`STAGE`/`RUN_ID` opt.)  | Live-tails the current/most recent agent run.   |
| `pipeline-brief`         | `TASK` (`STAGE`/`RUN_ID` opt.)  | Prints a compact summary of a run.              |
| `pipeline-verify`        | `TASK` (`BUILD=1` opt.)         | Runs build/test checks, writes a verification report. |
| `pipeline-usage`         | — (`TASK`/`AGENT`/`SINCE_HOURS` opt.) | Prints a usage/cost summary from the cross-task ledger, plus active cross-task cooldowns. |
| `pipeline-report`        | `TASK`                         | Prints a legible per-task report: stages, decision, verification, usage, reasoning traces. |

## Changelog

- **Phase 5** (2026-08-05): new `stream_events.reasoning_summary` (codex
  `reasoning`-typed `item.completed` items, claude `thinking_delta`
  content-block deltas accumulated per block index; agy always `None`, no
  known reasoning-bearing event in its schema); `real_runner.invoke_agent`
  gained an opt-in `capture_reasoning=True` kwarg — when reasoning is found
  and capture is enabled, writes `runs/<base>.reasoning.md` and adds
  `result["reasoning_path"]` to the metadata sidecar (purely additive,
  same precedent as Phase 4's `result["usage"]`; existing fixtures have no
  reasoning content, so no existing test/call site changed behavior);
  `tail.py`'s `brief()` gained `reasoning:`/`reasoning_path:` lines,
  mirroring the Phase 4 `usage:` line. New `config.py`
  `reasoning_capture.enabled` toggle (default on), validated like
  `usage_ledger`/`cross_task_cooldowns`; `controller.invoke_stage` wires it
  through. New `report.py` module — `generate_report(task_dir, task,
  state, usage_entries=None)` synthesizes the stage table (status/agent/
  duration/failure plus a short artifact excerpt), the Stage 8 decision,
  the Stage 5 verification report, `usage.summarize` totals for the task,
  and any captured reasoning traces into one document, written to
  `<task>/.orchestrator/task_report.{json,md}` and printed in full by the
  new `pipeline-report` command/target — the one thing a human reads
  end-to-end instead of running `pipeline-status`/`pipeline-verify`/
  `pipeline-usage`/`pipeline-brief` separately and merging by hand. 229
  tests (was 208 at end of Phase 4), all green throughout — no existing
  test needed behavioral changes. Confirmed real end-to-end against an
  actual task directory in `.agent-pipeline/tasks/` (7 completed stages);
  `.agent-pipeline/` stays entirely gitignored, so nothing this phase
  writes to a real task's `.orchestrator/` leaks into version control. See
  [phase-5-handoff.md](handoffs/phase-5-handoff.md).
- **Phase 4** (2026-08-05): new `usage.py` module — cross-task,
  cross-run-persistent `ledger.jsonl` (Layer 1: token/cost/duration/
  failure-class per real agent invocation, extracted from each CLI's own
  JSON stream via new `stream_events.usage_summary`) and
  `agent_cooldowns.json` (Layer 2: `usage_limit`/`rate_limit` failures
  recorded so every task's routing, not just the task that hit it,
  deprioritizes that agent until an extend-only-merged cooldown expires).
  `real_runner.invoke_agent` gained opt-in `task`/`ledger_path` params (every
  existing call site/test defaults to off, unaffected);
  `controller.choose_real_agent` reorders (never drops) fallback candidates
  via a stable partition over active cross-task cooldowns;
  `controller.mark_unavailable` gained an opt-in `cooldown_write` closure
  param that only the real driver's call site ever supplies, structurally
  guaranteeing the mock driver's 28 fixture scenarios can never write a
  cross-task cooldown; new `config.py` `usage_ledger`/`cross_task_cooldowns`
  toggles (both on by default); new `pipeline-usage` command/target; `tail.py`
  brief gained a `usage:` line. Confirmed real end-to-end: a `usage_limit`
  failure in one task's Stage 02 run gets a second, independent task's next
  `pipeline-run` to route Stage 02 to the fallback agent first, purely from
  the shared cooldown store. 177 → 208 tests, all green throughout — no
  existing test needed behavioral changes. Real CLIs never report a credible
  `reset_at` today, so cooldown windows are always
  `default_cooldown_seconds`-based in practice; real reset-time extraction,
  loosening `rate_limit`'s hard-block, a manual cooldown-clear command, and
  cost-budget *enforcement* were all explicitly scoped out — see
  [phase-4-handoff.md](handoffs/phase-4-handoff.md).
- **Phase 3** (2026-08-05): `verification.run_verification` wired into
  `run_real_pipeline` right after Stage 5's manifest is written
  (`check_concurrency_guard` gained an `allow_pid` bypass for this
  same-process re-entrant call); new `auto_verified` overseer route,
  deterministically applied by the controller
  (`overseer.upgrade_to_auto_verified`) from real verification evidence, never
  from the agent's own claim; new `config.py` `enable_auto_verified` toggle
  (default on) and `roles["07"]`/`turn_budgets["07"]`; new Stage 07 prompt
  branch in `prompts.py`; new `artifacts.manual_test_decision` outcome
  classifier; new controller-local Stage 08 decision synthesis
  (`ensure_stage08_decision`, "worst wins" over Stage 6's outcome and Stage
  7's verdict) — no agent call, matching how Stage 8 was already
  hand-checkbox-only in the legacy pipeline; `checkpoint_noop_eligible` fixed
  so a human completing Stage 6 by hand is no longer a permanent dead end —
  the next `pipeline-run` now drives Stage 7/8 automatically. Root-caused
  (did not need to touch `policies.py`/`controller.py`) and fixed the
  pre-existing `pipeline-mock-test` fixture drift flagged at the end of
  Phase 2: all four mismatches were `.agent-pipeline/fixtures/mock_scenarios.json`
  expectations predating the fallback/continuity-degraded-review resilience
  logic they're supposed to test, not a controller bug — `mock_pipeline` is
  now a trustworthy passing signal again. `followup-from-review` automation
  explicitly deferred (see "Known gaps"). 149 → 177 tests, all green
  throughout — no existing test needed behavioral changes beyond the fixture
  fix and one now-stale "known-failing" assertion. See
  [phase-3-handoff.md](handoffs/phase-3-handoff.md).
- **Phase 2** (2026-08-05): new `verification.py` (`unit_tests`,
  `mock_pipeline`, `gradle_compileJava`/`gradle_build`,
  `test_coverage_delta_signal`); `real_runner.py`'s subprocess-to-files
  logic extracted into a shared `run_to_files` helper reused by both
  `invoke_agent` and `verification.py`; `pipeline-verify` command; direct
  unit tests for previously only indirectly-covered pure functions
  (`build_argv`, `classify`, `extract_candidate`, `parse_overseer_candidate`,
  `fallback_handoff`). 83 → 149 tests, all green throughout — no existing
  test needed modification. Discovered (did not fix) a pre-existing
  `pipeline-mock-test` fallback-policy regression; see "Known gaps" above.
  See [phase-2-handoff.md](handoffs/phase-2-handoff.md).
- **Phase 1** (2026-08-05): real Stage 02-05 invocations switched to each
  CLI's JSON/stream-json output flag; new `stream_events.py`/`tail.py`
  modules; `pipeline-tail`/`pipeline-brief` commands; fixed a ~3s
  stdin-wait latency bug in claude/agy invocations. All changes additive
  with the pre-Phase-1 path preserved as fallback — no existing test
  needed modification. See
  [phase-1-handoff.md](handoffs/phase-1-handoff.md).
- **Phase 0** (2026-08-05): baseline docs created; `pipelineBugs.md` retired
  (all 8 bugs confirmed fixed); `Makefile.legacy` annotated as rollback-only.
