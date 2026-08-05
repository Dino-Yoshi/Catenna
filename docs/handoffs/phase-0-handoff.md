# Phase 0 handoff — Docs & hygiene baseline

**Status:** done (2026-08-05)

## What changed

- Created `docs/agent-pipeline/OVERVIEW.md` — living architecture doc
  (current pre-redesign state of both pipelines, state machine, known gaps,
  command table).
- Created `docs/agent-pipeline/PHASES.md` — phase tracker and the binding
  conventions (single entry point, test-explicit, no retroactive rewrites,
  mandatory handoff docs) that apply to every later phase.
- Created `docs/agent-pipeline/handoffs/` (this file is the first entry).
- Removed `pipelineBugs.md` from the repo root. It was untracked in git (not
  `git rm`-able; plain `rm`). All 8 bugs it documented were verified fixed in
  current code before deletion:
  - Stage 6 completion validation (BUG-1/4) — `artifacts.py`'s
    `validate_manual_test_outcome` requires an explicit checkbox/outcome.
  - `pipeline-dry-run` terseness (BUG-2/5) — `controller.dry_run` prints
    per-artifact stage/status/reason/staleness.
  - Unittest discovery finding 0 tests (BUG-3/6) —
    `python3 -m unittest discover -s tools/agent_pipeline/tests` runs 48
    tests, confirmed passing (see Verification below).
  - Asterisk-prefixed checkboxes rejected (BUG-7) — regex in `artifacts.py`
    accepts `[-*+]\s*\[[xX]\]`.
  - `completed_stages` not dependency-prefix aware (BUG-8) —
    `state.contiguous_completed` walks `STAGE_ORDER` and stops at the first
    invalid stage.
- Added a header comment to `Makefile.legacy` clarifying it's a rollback
  reference, that `Makefile.orchestrator` is canonical for Stages 00-05, and
  that it remains the only automation for Stages 6-8 until Phase 3 lands.
  `Makefile.legacy` was **not** deleted or merged — deferred until no task is
  mid-flight on its stage targets.

## Why

Establish the shared vocabulary/doc structure the rest of the redesign
builds on (per the approved plan's "multi-session handoff convention"), and
clear out a stale bug list that could otherwise mislead a future session into
re-fixing already-fixed problems.

## How to verify

```
python3 -m unittest discover -s tools/agent_pipeline/tests
# Ran 48 tests ... OK
test -f docs/agent-pipeline/OVERVIEW.md && test -f docs/agent-pipeline/PHASES.md
test ! -f pipelineBugs.md
head -8 Makefile.legacy   # confirm the new header comment is present
```

## Deliberately deferred

- The missing live `.agent-pipeline/config/orchestrator.json` issue (real
  pipeline runs are silently on `DEFAULT_CONFIG`) is flagged in
  `OVERVIEW.md`'s "Known gaps" section but not fixed — it's outside this
  redesign's core scope.
- `Makefile.legacy` deletion/merge — deferred to Phase 3, once Stages 6-8 are
  orchestrator-driven and no task is relying on the legacy targets.

## Important discovery: the entire pipeline is gitignored, on purpose

`Makefile`, `Makefile.legacy`, `Makefile.orchestrator`, `tools/agent_pipeline/`,
and `.agent-pipeline/` are all in `.gitignore` (see the "Local agent pipeline
artifacts" / "Local dev-pipeline docs/notes" sections) — this was deliberate,
done during the "Clean up repo for public release" pass, so this whole
orchestration system stays local dev tooling and never lands in the public
repo. `docs/agent-pipeline/` (this doc structure) has been added to
`.gitignore` too, to stay consistent — otherwise it would have been the only
publicly-committed trace of an otherwise-fully-hidden system. **None of this
redesign's work should be committed to the public history**; that matches
existing practice (`AGENTS.md`/`CLAUDE.md`/`ISSUES.md` are gitignored the same
way) and needs no further action, just awareness for future sessions.

## Notes for the next session (Phase 1)

- No code behavior changed in this phase — only docs, one file deletion, and
  one comment addition. Safe to start Phase 1 immediately.
- Phase 1's first step is a **spike**: confirm whether `codex`/`claude`/`agy`
  CLIs actually flush stdout incrementally when not attached to a tty, before
  building any streaming/tee mechanism in `real_runner.py`. If they fully
  buffer until exit, "live" streaming from the parent process is not
  achievable without a different invocation mode (e.g. a CLI's own
  `--json`/verbose event-stream flag, if one exists) — check each CLI's
  `--help` output for this before assuming a tee on the file handles will
  show anything before the process exits.
