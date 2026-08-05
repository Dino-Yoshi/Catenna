# Phase 5 handoff — Legible reporting + reasoning trace capture

**Status:** done (2026-08-05)

## What changed

- **`tools/agent_pipeline/stream_events.py`**: new `reasoning_summary(agent,
  stdout_text)`, same idiom as `usage_summary`/`final_text` (auto-detects
  agent, returns `None` on anything unrecognized/absent, never raises).
  Codex: accumulates `item.completed` events where `item.type ==
  "reasoning"`, taking `item.text`. Claude: accumulates `stream_event` →
  `content_block_delta` events whose `delta.type == "thinking_delta"`,
  keyed by `content_block_start`'s index so concurrent blocks can't
  interleave, joined per block then joined across blocks with a blank
  line. Agy: always `None` — no known reasoning-bearing event in its
  stream-json schema today (its `step_update` events carry no reasoning
  text), documented as a known gap the same way agy's usage extraction was
  flagged unconfirmed in Phase 4.
- **`tools/agent_pipeline/real_runner.py`**: `invoke_agent(...)` gained a
  trailing optional `capture_reasoning=True` kwarg, same gating pattern as
  the existing `ledger_path=None` param — `reasoning_summary` is always
  computed, the flag only gates whether the sidecar file gets written.
  When reasoning text is found and capture is enabled, writes
  `runs/<base>.reasoning.md` (a small markdown header plus the raw text)
  and adds `result["reasoning_path"]` to the metadata sidecar (purely
  additive field, same precedent as Phase 4's `result["usage"]`). Every
  pre-Phase-5 fixture/test stream has no reasoning content, so
  `reasoning_summary` returns `None` for all of them — no behavioral
  change for any existing call site or test.
- **`config.py`**: new `reasoning_capture: {"enabled": True}` in
  `DEFAULT_CONFIG`, validated the same shape-check way as
  `usage_ledger`/`cross_task_cooldowns`. Rollback switch, same role as
  `enable_auto_verified`.
- **`controller.py`**: `invoke_stage` reads
  `config.get("reasoning_capture", {}).get("enabled", True)` and passes it
  through as `capture_reasoning`, next to the existing `ledger_path` line.
  New `pipeline_report(task)` — loads+reconciles state the same way
  `status()`/`dry_run()` do, filters the usage ledger to this task's
  entries, calls `report.generate_report(...)`, and prints the **full
  rendered markdown** to stdout (not a terse summary line — the whole
  point of this command is to be the one document a human reads
  end-to-end). Always `EXIT_SUCCESS` (read-only reporting).
- **`tail.py`**: `brief()` gained `reasoning_path:` (always, when present)
  and `reasoning:` (a truncated excerpt, when the referenced file is
  readable) lines, mirroring the `usage:` line Phase 4 added. Read via a
  new best-effort `_safe_read` helper — never raises if the file went
  missing.
- **`tools/agent_pipeline/report.py`** (new module): structured exactly
  like `verification.py` — `generate_report(task_dir, task, state,
  usage_entries=None)` returns a dict with `report_paths` after writing
  `<task>/.orchestrator/task_report.{json,md}`; `render_markdown`/
  `write_report` are separately testable helpers. Deliberately reads
  `task_dir` directly for anything task-local (state's `artifact_status`/
  `stage_agents`/`real_stage_runs`/`fallback_events`, the stage artifact
  files themselves, `05_verification_report.json`) rather than depending
  on `controller.py`'s path constants, and takes ledger entries as a
  parameter rather than reading the ledger itself — the same decoupling
  `verification.py`/`usage.py` already use to avoid a dependency on
  `controller`/`TASKS_ROOT`/`USAGE_ROOT`. Builds:
  - a per-stage table (status/reason from `artifact_status`, agent from
    `stage_agents`, duration/failure_class from the last
    `real_stage_runs[stage]` entry, plus a short generic excerpt — first
    ~300 chars after the artifact's required top-level heading,
    whitespace-collapsed — that works across all 8 contracts' differing
    section names without special-casing each one);
  - the Stage 8 decision (reusing `artifacts.manual_test_decision`, the
    same helper `status()` already calls, plus an excerpt of the "Reason"
    section via `artifacts.extract_section`);
  - the Stage 5 verification report, read directly from
    `05_verification_report.json` if present;
  - usage totals via `usage.summarize(usage_entries or [],
    group_by="agent")` — Phase 4's `usage.py` needed zero changes;
  - reasoning traces, found by globbing `tail.runs_dir(task_dir)` for
    `*.reasoning.md` and pairing each with its `.json` metadata sidecar
    (same base-name convention as `.stdout`/`.json`) for stage/agent/
    run_id context;
  - fallback/retry history straight from `state["fallback_events"]`.
- **`cli.py`** / **`Makefile.orchestrator`**: new `pipeline-report`
  command/target (`--task`/`TASK` only), following the exact pattern of
  every other `add_task(...)`-wrapped subcommand.

## Why

PHASES.md's Phase 5 line — "legible reporting + peer into thinking
(reasoning trace capture)" — was the only scoping that existed going in.
Both halves were ambiguous enough to need a decision before writing code
(same situation Phase 4 was in). I asked the user directly rather than
guessing:

- **Reasoning capture**: persist per run (new sidecar file), not a
  live-tail change. Chosen over "live tail only" (a human debugging a
  specific run after the fact is a more common use case than watching
  reasoning stream live) and over "live tail too" (scope creep this
  phase — live-tail reasoning display is a separate, deferred piece of
  work, see below).
- **Legible reporting**: a per-task report, not a cross-task dashboard.
  Chosen because today's four separate commands
  (`status`/`verify`/`usage`/`brief`) already cover a single task
  reasonably well individually — the missing piece is synthesis, not a
  new data source. A cross-task dashboard is a genuinely different
  feature (task triage across `.agent-pipeline/tasks/`) explicitly
  deferred to a future phase, same as Phase 4 deferred cost-budget
  *enforcement* after building the ledger that would make it possible.

## Design decisions worth knowing

- **Reasoning trace capture is real-driver-only.** `MockAgent` never
  produces `.stdout` in any of the three real CLIs' JSONL schemas, so
  mock-run tasks never get a `*.reasoning.md` file — exactly parallel to
  how Phase 4's usage ledger only ever gets entries from real runs, not
  mock scenarios.
- **Computation vs. persistence stay separate**, matching the
  `ledger_path=None` precedent: `reasoning_summary` always runs (cheap,
  pure JSONL parsing already done for `usage_summary`), only the file
  write is gated by `capture_reasoning`. This means turning the config
  toggle off can't hide a bug in extraction itself during testing/review.
- **The stage-table excerpt is deliberately generic, not per-contract.**
  Rather than teaching `report.py` each of the 8 `ArtifactContract`s'
  differing section names (Stage 1's "Objective" vs. Stage 2's "Summary"
  vs. Stage 5's "Summary of changes", etc.), it just strips the required
  top-level heading line and takes the first ~300 characters of what's
  left. Less precise than a per-contract "grab the Summary section"
  extractor would be, but it's one code path instead of eight, and it
  degrades gracefully if a contract's sections ever change shape.
- **`task_report.{json,md}` lives under `.orchestrator/`, not among the
  numbered `00`-`08` artifacts.** It's derived observability output, not a
  pipeline-contract artifact — it must never be subject to
  `ArtifactContract` validation, never appear in `reconcile_artifacts`'s
  `STAGE_ORDER` walk, and never be treated as an input a future stage
  depends on. Same rationale as `05_verification_report.{json,md}` living
  in the task root (verification predates this convention) vs. `state.json`/
  `log.jsonl`/`runs/` living in `.orchestrator/` — this phase followed the
  `.orchestrator/`-for-derived-output precedent since there's no format
  compatibility reason (unlike verification, which was Phase 2/pre-dates
  this call) to do otherwise.
- **`pipeline_report` prints the full rendered markdown, not a terse
  summary line.** Every other reporting command
  (`status`/`dry-run`/`pipeline-verify`/`pipeline-usage`) prints a
  condensed console summary and leaves the detail in a file. This command
  is different on purpose: its entire reason to exist is to be the one
  thing a human reads end-to-end instead of running four commands and
  merging the output by hand, so the console output *is* the deliverable,
  not a pointer to one.

## How to verify

```
python3 -m unittest discover -s tools/agent_pipeline/tests
# Ran 229 tests ... OK  (was 208 at end of Phase 4)

make -f Makefile.orchestrator pipeline-mock-test
# mock tests passed: 28

make -f Makefile.orchestrator pipeline-report TASK=<any real task under .agent-pipeline/tasks/>
# prints a full markdown report: stage table with excerpts, decision,
# verification, usage, reasoning traces, fallback history

ls .agent-pipeline/tasks/<task>/.orchestrator/task_report.* 2>&1
# confirms both files were written

git status --short
# .agent-pipeline/ is gitignored end-to-end, so running pipeline-report
# against a real task never produces anything to commit
```

New/extended test coverage this phase: `tests/test_stream_events.py`
(`ReasoningSummaryTests` — codex reasoning extraction, claude
multi-delta accumulation in order, agy/plain-text/no-reasoning-stream all
return `None`, auto-detect without an explicit agent);
`tests/test_real_runner_streaming.py` (reasoning-bearing stream writes
the sidecar and sets `result["reasoning_path"]`; non-reasoning stream
writes nothing; `capture_reasoning=False` computes but doesn't write);
`tests/test_tail.py` (`brief()` prints/omits the reasoning lines
correctly, doesn't raise when the referenced file is missing);
`tests/test_config.py` (new file — `reasoning_capture` validated the same
way as `usage_ledger`/`cross_task_cooldowns`; also backfills a
previously-missing direct `validate_config`/`DEFAULT_CONFIG` test, which
didn't exist before this phase for any of the three toggles); new
`tests/test_report.py` (empty task → placeholders, no crash; fully
populated fake state + real artifact/verification/reasoning files on disk
→ every markdown section covered; missing/invalid decision file → `None`,
not a crash; `report_paths` files actually written under `.orchestrator/`).

## Known gaps

- `reasoning_summary`'s agy handling isn't merely unconfirmed like agy's
  usage extraction was in Phase 4 — there's no known reasoning-bearing
  event in agy's schema at all today, so agy reasoning traces are never
  captured, repo-wide, until a real fixture surfaces one.
- Reasoning capture is real-driver-only (see "Design decisions" above) —
  not fixable without teaching `MockAgent` to emit real CLI JSONL shapes,
  which is out of scope for a deterministic test harness.
- `pipeline-tail`'s live view still only prints "thinking..." for claude
  (unchanged from Phase 1) — no live streaming of actual reasoning text.
  Scoped out this phase per the user's explicit choice (capture+persist,
  not live tail).
- No cross-task dashboard — `pipeline-report` is strictly per-task. A
  future phase could reuse `usage.summarize(entries, group_by=...)` (any
  key, not just `"agent"`) and a `.agent-pipeline/tasks/` directory walk
  to build one, but "what counts as stuck/needs-attention" is undefined
  and belongs in an explicit follow-up the user asks for.
- The stage-table excerpt is generic (first ~300 chars after the heading),
  not a per-contract "grab the most meaningful section" extractor — see
  "Design decisions" above for the tradeoff.

## Deliberately deferred

- Live-tail reasoning streaming (real text, not just a "thinking..."
  status line).
- Real agy reasoning/thinking event schema — depends on a fixture that
  wasn't available this session, same situation as agy's usage extraction
  in Phase 4.
- Cross-task dashboard / task-triage view across `.agent-pipeline/tasks/`.
- Per-contract-aware excerpt extraction (pulling each stage's actual
  "Summary"/"Objective"/etc. section instead of a generic post-heading
  excerpt).

## Notes for the next session (Phase 6)

- PHASES.md has no Phase 6 defined yet — this was the last phase
  explicitly scoped in the original redesign plan. A fresh session picking
  this up should check with the user for what's next rather than assuming
  one of the "Known gaps"/"Deliberately deferred" items above is
  automatically in scope.
- `report.py`'s `generate_report`/`render_markdown` split (data dict vs.
  markdown rendering) is reusable as-is if a future cross-task dashboard
  wants to render multiple tasks' summaries into one document — it would
  likely call `generate_report` per task and fold the dicts together
  rather than needing new extraction logic.
- `usage.summarize(entries, group_by=...)` (Phase 4) already accepts
  `"task"` as a group dimension, which is exactly what a cross-task
  dashboard's usage rollup would need — no `usage.py` changes anticipated
  for that follow-up.
- Full suite: `python3 -m unittest discover -s tools/agent_pipeline/tests`
  (229 tests, all green). No new controller-module-global root was added
  this phase (unlike Phase 4's `USAGE_ROOT`), so the `TASKS_ROOT`/
  `USAGE_ROOT` monkeypatch gotcha documented in the Phase 4 handoff
  doesn't have a new counterpart to repeat here — `report.py` takes
  everything it needs as parameters instead of owning a path constant.
