# Phase 1 handoff — Live visibility into running agents

**Status:** done (2026-08-05)

## What changed

- **New module `tools/agent_pipeline/stream_events.py`** — single source of
  truth for the three CLIs' JSONL event schemas: `detect_agent`/
  `detect_agent_from_stream` (content-sniffed: `"event"` key → agy,
  `type=="thread.started"` → codex, any other `"type"` key → claude),
  `final_text` (pulls the finished response text out of a JSONL stream),
  `structured_failure` (best-effort structured failure classification),
  `summarize_event` (one human-readable line per structural event, `None`
  for events not worth printing, never raises on unrecognized shapes).
- **`real_runner.py`**:
  - `build_argv` now requests structured streaming from all three CLIs:
    `--json` for codex, `--output-format stream-json
    --include-partial-messages --verbose` for claude, `--output-format
    stream-json` for agy. Purely additive flags.
  - Fixed a real latency bug found during the spike: claude/agy print-mode
    subprocess calls now get `stdin=subprocess.DEVNULL` instead of
    inheriting the parent's stdin (which caused claude to burn ~3s per
    call waiting for stdin it never needed — confirmed with a live timed
    call before and after).
  - `extract_candidate`/`classify` both try `stream_events` first
    (`final_text`/`structured_failure`) and fall back to the exact
    pre-Phase-1 logic (raw-stdout dump / substring search) when it returns
    `None`. This is why none of the ~48 Phase-0 tests needed changes: the
    fake-agent fixtures emit plain text, `stream_events` finds no
    parseable JSON, and both functions fall straight through unchanged.
- **New module `tools/agent_pipeline/tail.py`** — read-only, no locking, no
  state mutation (same posture as `controller.status`/`dry_run`):
  `locate` (picks the newest in-progress run, or newest completed run, or
  an explicit `--stage`/`--run-id` match), `follow` (polls a `.stdout`
  file for growth, prints `summarize_event` output per new line, stops
  when the `.json` sidecar appears or on `KeyboardInterrupt`), `brief`
  (compact one-shot summary: agent/stage/duration/exit code/failure
  class/event-type counts/final-text excerpt).
- **`controller.py`**: thin wrappers `pipeline_tail`/`pipeline_brief`
  delegating to `tail.py`.
- **`cli.py`** / **`Makefile.orchestrator`**: new `pipeline-tail --task T
  [--stage S] [--run-id R]` and `pipeline-brief --task T [--stage S]
  [--run-id R]`, both CLI subcommand and Makefile target added together.
- **`docs/agent-pipeline/OVERVIEW.md`**: command table updated; "no live
  visibility" gap replaced with a note that granularity varies by agent
  and that failure classification is additive.

## Why

Phase 0 flagged that "live" visibility was unproven — the CLIs might fully
buffer stdout regardless of any tee mechanism we built. This phase started
with exactly that spike (see "Live-spike findings" below), which showed the
premise was correct: default text-mode output from both `codex exec` and
`claude -p` is fully buffered (confirmed live: a 10s/5-line response arrived
as a single write at the very end for both CLIs). Only the JSON/stream-json
flags flush incrementally, which meant the *actual* stage invocation had to
change, not just the tailing tool. That's why the design threads
`stream_events` through `extract_candidate`/`classify` as an additive layer
with the old behavior as guaranteed fallback, rather than a parallel/shadow
invocation (which would have doubled real API cost per stage and still not
been live).

## Live-spike findings (this session, against the actual installed CLIs)

- `codex exec` default text mode: fully buffered (all output at process
  exit).
- `claude -p` default text mode: fully buffered (all output at process
  exit).
- `codex exec --json`: incremental (first JSONL event before turn
  completion).
- `claude --output-format stream-json --include-partial-messages
  --verbose`: true token-level deltas, ~15 events over a 10s call.
- `agy --output-format stream-json`: `step_update` events per step,
  incremental.
- Final-text field locations: codex `item.completed` item
  (`item.type=="agent_message"`) → `item.text` (not used for extraction —
  codex already writes the final message via `--output-last-message`
  regardless of `--json`); claude `result` event → `result.result`; agy
  `event:"result"` → `result.response`.
- `agy`'s prompt must be passed via the same `-p <text>`/`--prompt <text>`
  mechanism `detect_agy_prompt_mode` already selects — passing `-p` as a
  bare flag with the prompt as a separate trailing arg gets misparsed as a
  question *about* the flag itself, not the actual prompt (this is a
  pre-existing quirk of `agy`'s CLI, not something Phase 1 changed;
  `build_argv`'s existing `["-p", prompt_text]` pairing was already
  correct).

## How to verify

```
python3 -m unittest discover -s tools/agent_pipeline/tests
# Ran 83 tests ... OK  (was 48 at end of Phase 0)

# Live end-to-end proof (not part of the automated suite — costs a real
# API call): invoke_agent() directly against real claude in a background
# thread while tail.follow() watches the same task_dir from the main
# thread. Observed this session:
#   t=0.70s  session started
#   t=1.90s  responding...
#   t=2.90s  result: <final text>
#   t=3.30s  run complete
# i.e. progress visible ~1.2s into a 3.3s run, not only at the end.
```

Manual CLI smoke test against a real task (costs a real API call per
stage; not run this session to avoid mutating a real task's pipeline
state — see "Deliberately deferred"):

```
make -f Makefile.orchestrator pipeline-run TASK=<task> ALLOW_DIRTY=1   # terminal 1
make -f Makefile.orchestrator pipeline-tail TASK=<task>                # terminal 2, while stage 1 is running
make -f Makefile.orchestrator pipeline-brief TASK=<task>
```

## Deliberately deferred

- Did not run a full `pipeline-run` against any real task directory under
  `.agent-pipeline/tasks/` this session — Stage 05 is workspace-write and
  could have caused a real coding agent to edit repo files unattended.
  Instead validated the exact same code path (`real_runner.invoke_agent`
  + `tail.follow`) directly against a scratch task directory outside
  `.agent-pipeline/tasks/`, in read-only mode. The `pipeline-tail`/
  `pipeline-brief` CLI subcommands themselves were exercised via the full
  argparse → controller → tail.py chain in `test_real_runner_streaming.py`
  and `test_tail.py`, just not via an actual `make -f Makefile.orchestrator
  pipeline-run` invocation. A future session (or the user) doing a real
  task run should treat that as the first live confirmation of the
  Makefile-level wiring end to end.
- `codex`'s `turn.failed` structured-failure shape in
  `stream_events.structured_failure` is a best-effort guess (never
  observed a real codex max-turns failure live — only synthesized in
  tests). If it turns out to be wrong, the substring fallback in
  `classify()` still catches it (unchanged, always runs when the
  structured path returns `None`), so this is a soft gap, not a
  regression risk.
- No coalescing/rate-limiting of `pipeline-tail` output beyond what
  `summarize_event` already does structurally (skip raw text deltas,
  print once per structural transition) — not needed at observed event
  volumes (~10-15 events over a several-second real call).
- Reasoning-trace capture ("peer into thinking") — Phase 5.
- `runs/` directory rotation/pruning — not requested, out of scope.

## Notes for the next session (Phase 2)

- No existing test or behavior needed modification — every Phase 1 change
  was additive with the pre-Phase-1 path preserved as fallback. Full
  suite: `python3 -m unittest discover -s tools/agent_pipeline/tests`
  (83 tests).
- `stream_events.py` is now the single place that knows the three CLIs'
  JSON schemas. Phase 2's `verification.py` (build/test report,
  test-coverage-delta signal) and Phase 3's overseer-driven review will
  likely want richer structured data from Stage 5 runs (e.g. tool-call
  lists, not just final text) — extend `stream_events.py` rather than
  re-parsing JSONL elsewhere.
- If a future session wants a real end-to-end Makefile-level smoke test
  (`pipeline-run` + `pipeline-tail` in two terminals), create a disposable
  task under `.agent-pipeline/tasks/` for it rather than reusing a real
  in-flight task, and expect Stage 05 to actually invoke a workspace-write
  agent — don't run it unattended against files you care about.
