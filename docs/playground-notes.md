# Playground notes

Running log of issues found while live-testing `catenna` against a real
driven project (`/home/zero/Documents/prog/catennaHelloWorld`, a
deliberately tiny "playground" repo used only to smoke-test the tool
itself). Newest entries at the bottom.

## 2026-08-08: dirty-worktree block message tells you to set an env var that doesn't exist

`catenna run <task>` blocks Stage 5 when the source tree isn't clean via
`run_real_pipeline` (`controller.py:499`), and the printed message says to
rerun with `ALLOW_DIRTY=1`. That variable is never read anywhere in the
codebase (grepped — `controller.py:499` is the only hit). The actual
bypass is the `--allow-dirty` CLI flag already wired up in `cli.py`. A
user who does what the message says gets no effect and stays blocked with
no explanation.

Fixed 2026-08-08: the live Stage 5 dirty-worktree block now names
`--allow-dirty`.

## 2026-08-08: `catenna run`/`catenna verify` block the terminal for the whole call

Both commands drive every stage synchronously and in-process —
`run_to_files()` calls `subprocess.Popen(...).communicate(timeout=...)`
per stage — with no way to back off. `catenna verify --build` can block
for up to 30 minutes (the `gradle build` timeout). Neither subparser has
a daemonize/background option. `run` has a working `catenna tail`/
`catenna status` from a second terminal, and unfiltered `catenna tail`
can now follow verification stdout from `.orchestrator/verification_runs/`.
Core complaint: a single-terminal workflow has no way to kick either off
and then do anything else, including check on it, until it finishes.

Fixed 2026-08-08: `run` and `verify` now accept `--background` and write
detached output to per-task `.orchestrator/background_*.log` files.

## 2026-08-08: `catenna --help` shows the wrong usage line for how it was actually invoked

`build_parser()` hardcodes `prog="python3 -m agent_pipeline.cli"`
(`cli.py:20`). That was correct before the `catenna` console-script entry
point shipped, but wasn't revisited after. `catenna --help` (and every
subcommand's `--help`, and `catenna help [command]`, which shares the
same parser) prints `usage: python3 -m agent_pipeline.cli ...` even when
invoked as `catenna`. Argparse error messages use the same `prog` and are
equally affected.

Fixed 2026-08-08: help/usage shows `catenna` for the console script and
keeps `python3 -m agent_pipeline.cli` for module-form invocation.

## 2026-08-09: `catenna tail` now follows a task's whole lifecycle, into `verify` too

Follow-up to the 2026-08-08 "block the terminal" entry above, whose
"verify has no live-tail equivalent at all today" gap is now closed.
`tail.follow()` used to stop after a single pipeline stage and never
looked at `.orchestrator/verification_runs/` at all. Unfiltered `catenna
tail <task>` now auto-advances across pipeline stages and into
verification checks, rendering `verification_runs/` output as raw lines
(it's plain gradle/pytest/unittest stdout, not agent-stream JSONL) and
stopping once the task reaches a terminal/paused state or
`05_verification_report.md` is written. Getting there required adding
completion `.json` sidecars to `verification.py`'s four check functions,
since `run_to_files()` never wrote one on its own (a gap not present in
the original idea, found while speccing this).
