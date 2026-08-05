# Phase 4 handoff — Usage ledger + persisted cross-task cooldowns

**Status:** done (2026-08-05)

## What changed

- **`tools/agent_pipeline/stream_events.py`**: new `usage_summary(agent,
  stdout_text)`, same idiom as `structured_failure`/`final_text` (auto-detects
  agent, iterates the parsed JSONL stream, returns `None` on anything
  unrecognized, never raises). Extracts codex's `turn.completed` event's
  `usage` object, claude's `result` event's `usage`/`total_cost_usd` fields,
  and a best-effort look at agy's `result.usage`/`result.total_cost_usd`/
  `result.cost_usd` (no confirmed real fixture for agy — degrades to `None`
  silently, same as every other agy path in this module). A small
  `_normalize_usage` helper produces one shape regardless of source CLI
  (`input_tokens`, `output_tokens`, `cache_read_tokens`,
  `cache_creation_tokens`, `total_cost_usd`) so callers never branch per-agent.
- **`tools/agent_pipeline/usage.py`** (new module): owns two on-disk stores
  under a new `.agent-pipeline/usage/` root — `ledger.jsonl` (Layer 1) and
  `agent_cooldowns.json` (Layer 2). Deliberately has no dependency on
  `controller`/`TASKS_ROOT` (avoids a circular import, since `real_runner.py`
  — which calls into this module — sits below `controller.py`). Every public
  function is best-effort: writers swallow all exceptions and return `False`,
  readers degrade to `[]`/`{}` rather than raising, matching
  `stream_events.py`'s existing "never raise" philosophy. Ledger appends use
  an `fcntl.flock`-guarded single `write()`; the cooldown store uses a
  separate `.lock` file (flock) around a read-merge-write critical section,
  with the actual file replaced via `tmp + os.replace` (mirrors
  `state.py::write_state_atomic`) — decoupling mutual exclusion from
  crash-safe replacement, since `os.replace` swaps inodes and would silently
  drop a lock held on the pre-replace fd.
- **`tools/agent_pipeline/real_runner.py`**: `invoke_agent(...)` gained
  trailing optional kwargs `task=None, ledger_path=None` (every existing call
  site and every existing test kept its default, so nothing besides the new
  tests exercises the ledger path). Computes `usage_summary(...)`, adds it as
  `result["usage"]` (purely additive field in the existing
  `runs/<base>.json` metadata sidecar), and appends a ledger entry via
  `usage.build_entry`/`usage.append_entry` when `ledger_path` is given.
- **`tools/agent_pipeline/controller.py`**:
  - New `USAGE_ROOT` constant (next to `TASKS_ROOT`/`FIXTURES_ROOT`) and
    `usage_ledger_path()`/`cooldown_store_path()` helpers that read it
    late-bound — the same pattern that already lets tests monkeypatch
    `TASKS_ROOT`, now extended to `USAGE_ROOT`.
  - `invoke_stage` passes `task=state["task"]` and a ledger path (or `None`
    if `usage_ledger.enabled` is `false`) into `invoke_agent`.
  - `status()` best-effort-prints a `cross_task_cooldowns:` line
    (`try/except Exception: pass`, so a missing/corrupt store can never break
    `status`) alongside the existing `run_unavailable_agents:` line.
  - New `pipeline_usage(task=None, agent=None, since_hours=None)`: reads the
    ledger, filters, prints per-agent and overall counts/duration/tokens/cost
    via `usage.summarize`, plus current cross-task cooldowns. Always
    `EXIT_SUCCESS` (read-only reporting, matches `pipeline-status`/
    `pipeline-brief`).
  - `mark_unavailable` gained an optional `cooldown_write=None` parameter.
    Every pre-existing call site (all three live in the **mock** driver:
    `run_stage`'s two `usage_limit`/`rate_limit` branches and
    `handle_failure`'s `sandbox_environment` branch) passes zero new
    arguments and is byte-for-byte unchanged — this is what structurally
    guarantees `mock_test()`'s 28 fixture scenarios can never write a
    cross-task cooldown, rather than relying on a config check buried inside
    a shared function. Only the **real driver's** call site inside
    `ensure_real_stage` passes a closure over the new
    `record_cross_task_cooldown(config, state, agent, reason, reset_at)`,
    which itself no-ops unless `cross_task_cooldowns.enabled` is true and
    `reason` is `usage_limit` or `rate_limit` — explicitly excluding
    `source_failure` (command-not-found/local misconfiguration is not a
    quota signal, and cooling down an agent repo-wide because of one host's
    broken `$PATH` would be actively harmful).
  - `choose_real_agent` now calls `load_cross_task_cooldowns(config)`
    (best-effort, returns `{}` if disabled or the store is unreadable) and
    reorders `configured_candidates(...)` via `reorder_by_cooldown` — a
    stable partition that moves cooling candidates after non-cooling ones
    without dropping them, so a stage always still has somewhere to go even
    if every configured candidate happens to be cooling down. The returned
    route dict gained a `cross_task_cooldown_deferred` field for
    observability. `policies.py::choose_agent` (the **mock** driver's
    parallel routing function) is untouched — it has no `config` parameter
    today, and per the "route decision is deterministic, never
    agent-authored" invariant from Phase 3 plus this phase's own scope, the
    mock driver stays a pure, cross-task-state-free harness for testing the
    controller itself.
- **`config.py`**: two independently-toggleable additions to
  `DEFAULT_CONFIG` — `usage_ledger: {enabled: true}` and
  `cross_task_cooldowns: {enabled: true, default_cooldown_seconds: 900}` —
  functionally separate flags since Layer 1 (logging) and Layer 2 (routing)
  are independent concerns. Neither store's path is configurable;
  `USAGE_ROOT` stays a hardcoded Python constant like `TASKS_ROOT`/
  `CONFIG_PATH` always are, so a typo'd path in `orchestrator.json` can't
  scatter files outside `.agent-pipeline/`. `validate_config` gained a light
  sanity check for both sections. No `schema_version` bump, same precedent
  as Phase 3's `enable_auto_verified`.
- **`cli.py`** / **`Makefile.orchestrator`**: new `pipeline-usage` command
  (`--task`, `--agent`, `--since-hours`, all optional — a usage report is
  meaningful globally, so unlike most other targets this one doesn't require
  `TASK`) and matching Makefile target with new `AGENT`/`SINCE_HOURS` vars.
- **`tail.py`**: `brief()` now prints a `usage:` line when the run's
  metadata sidecar has a non-empty `usage` field.

## Why

Phase 4's stated goal was "smart agent/model routing + usage awareness."
Reading the code at the start of this phase showed there was zero usage/
cost/token tracking anywhere, and that agent "unavailability" from
`usage_limit`/`rate_limit` failures (`mark_unavailable`) lived only in one
task's `state.json`, reset every new run (`begin_new_run`) — so neither the
same task nor any other task remembered an agent was recently rate-limited.
The user's explicit answer to a scoping question this session ("Both,
ledger first") set the two-layer shape: build the usage ledger as the
foundation, then build persisted cross-task cooldowns and smarter fallback
ordering on top of it in the same phase. "Smart routing" here means
reordering fallback candidates by real, shared failure history — not
picking different LLM models by task difficulty, which was never the
intent once scoped down with the user.

## Design decisions worth knowing

- **Real CLIs never report `reset_at` today.** `real_runner.py`'s result
  dict has no `reset_at` field for any real agent invocation — only
  `mock_agent.py` populates it, for mock scenarios. This means
  `controller.credible_reset` is always `False` for the real driver, so a
  real `rate_limit` failure never reaches `mark_unavailable` — it always
  hard-blocks the task (unchanged this phase). Real `usage_limit` failures
  *do* reach `mark_unavailable`, but always with `reset_at=None`. Practical
  consequence: cross-task cooldown windows are always
  `default_cooldown_seconds`-based in practice for real runs, never a
  genuine CLI-reported reset time. Extracting a real reset time from CLI
  stdout/stderr text was explicitly scoped **out** this phase (no confirmed
  fixture for it, unlike the `usage` object which has one) — see "Known
  gaps."
- **Cooldown merge is extend-only.** `record_cooldown` takes
  `max(existing_expires_at, computed_expires_at)`, never shortening an
  existing window — so a task reporting a short default-cooldown moments
  after another task recorded a longer CLI-reported (or earlier
  default-based) window can't clobber it.
- **Cross-task cooldown deprioritizes, never hard-excludes.** This is the
  literal difference between "reordering" (this phase) and "filtering" —
  `reorder_by_cooldown` is a stable partition, not a filter, so routing
  capability is never strictly worse than before this phase even if every
  configured candidate for a role happens to be cooling down.
- **The route decision stays deterministic, never agent-authored** — this
  phase's cooldown reordering is pure controller code reacting to a real
  failure classification (`usage_limit`/`rate_limit`), same invariant Phase
  3 established for the `auto_verified` route.
- **`rate_limit`'s existing hard-block behavior is untouched this phase** —
  conservative choice, doesn't touch Stage 05 reproducibility more than
  necessary. A `default_cooldown_seconds`-based safety net existing now
  would make loosening this defensible in a future phase, but that's a
  separate behavioral change from what was asked for here.
- **No manual "clear a cooldown" CLI command this phase** — the cooldown
  store (`agent_cooldowns.json`) is a plain, human-editable JSON file if an
  operator needs to force-clear one early.
- **`USAGE_ROOT` is a hardcoded constant, not config-driven**, deliberately,
  to keep both stores structurally confined to `.agent-pipeline/usage/`.

## How to verify

```
python3 -m unittest discover -s tools/agent_pipeline/tests
# Ran 208 tests ... OK  (was 177 at end of Phase 3)

make -f Makefile.orchestrator pipeline-mock-test
# mock tests passed: 28

make -f Makefile.orchestrator pipeline-usage
# entries: 0 / overall: calls=0 failures=0 duration=0.0s  (empty ledger is fine)

ls .agent-pipeline/usage 2>&1
# No such file or directory -- confirms neither the real unit test suite
# nor pipeline-mock-test leaked any file into the real repo's usage store;
# this was checked by hand after every test-writing step this phase, not
# just once at the end, since it's exactly the hazard the opt-in-via-closure
# design (mark_unavailable's cooldown_write param) exists to prevent
# structurally rather than by convention.
```

New/extended test coverage this phase: `tests/test_usage.py` (new — ledger
roundtrip, a genuine cross-*process* concurrency smoke test spawning several
subprocesses that each append many entries and asserting the exact expected
line count, cooldown extend-not-shorten merge, expired-entry exclusion,
corrupt-file degradation); `tests/test_stream_events.py` (usage extraction
per CLI, graceful degradation); `tests/test_real_runner_streaming.py`
(usage populates `result["usage"]` and the ledger; `ledger_path=None`
writes nothing); `tests/test_real_pipeline.py` (real end-to-end proof: a
`usage_limit` failure on Stage 02 in one task records a cross-task cooldown,
and a **second, fresh task** run afterward has that agent reordered behind
its fallback for Stage 02 even though its own `run_unavailable_agents` is
empty — the actual proof of cross-task reordering, not just unit-level
plumbing; plus both `enabled: false` toggle tests);
`tests/test_controller_reliability.py` (`status()` prints cooldowns when
active, stays silent and never raises when the store is missing/corrupt);
`tests/test_tail.py` (`brief()` prints/omits the `usage:` line correctly).

## Known gaps

- Real CLI `reset_at` extraction is not implemented — cross-task cooldown
  windows are always `default_cooldown_seconds`-based (900s default) for
  real runs today, never a genuine CLI-reported reset time. Revisit if a
  confirmed real fixture for reset-time text ever turns up (parallel to how
  `usage_summary`'s codex/claude extraction only happened once a confirmed
  fixture existed).
- `rate_limit` without a credible reset still hard-blocks a task outright
  (`ensure_real_stage`'s existing behavior, untouched). A future phase could
  reconsider this now that a cross-task cooldown safety net exists, but that
  wasn't this phase's scope.
- No manual cooldown-clear command (`pipeline-clear-cooldown` or similar) —
  deferred; the store is a plain JSON file an operator can hand-edit.
- agy's usage/cost extraction in `stream_events.usage_summary` is
  unconfirmed against a real fixture (no confirmed real agy `result` event
  usage schema was available to verify against, unlike codex/claude) — it
  will silently return `None` for agy today unless agy's real output
  happens to match the speculative `result.usage`/`result.total_cost_usd`/
  `result.cost_usd` field names guessed here.
- No live `.agent-pipeline/config/orchestrator.json` still exists (carried
  over from Phase 2/3's "Known gaps," not addressed here) — real pipeline
  runs remain silently on `DEFAULT_CONFIG`, which now includes this phase's
  `usage_ledger`/`cross_task_cooldowns` defaults (both on).

## Deliberately deferred

- Real reset-time parsing from CLI text (see "Known gaps").
- Loosening `rate_limit`'s hard-block behavior.
- A manual cooldown-clear CLI command.
- Any actual cost-budget *enforcement* (hard-capping an agent because it's
  "too expensive") — this phase is observability (the ledger) plus
  failure-driven cooldown routing, not budget policy. What counts as "too
  much" spend is undefined and belongs in an explicit follow-up the user
  asks for, not something to invent here.
- Real per-token cost accuracy for agy — depends on a real fixture that
  wasn't available this session.

## Notes for the next session (Phase 5)

- Phase 5 is "legible reporting + peer into thinking (reasoning trace
  capture)." The usage ledger (`usage.py`, `pipeline_usage`) is a natural
  place to extend for reasoning-trace-adjacent reporting, but its schema
  (`build_entry`) currently only carries stage/agent/duration/exit/failure/
  usage — extending it for trace data should stay additive, same pattern as
  this phase's `result["usage"]` field.
- `usage.summarize(entries, group_by=...)` accepts any entry key as the
  group dimension (`"task"`, `"stage"`, not just `"agent"`) — reusable as-is
  if Phase 5's reporting needs a different breakdown.
- `reorder_by_cooldown`/`load_cross_task_cooldowns`/
  `record_cross_task_cooldown` in `controller.py` are small, pure/
  near-pure functions with no agent dependency — safe to extend if a future
  phase wants cooldown duration to vary by failure type or agent rather than
  one repo-wide `default_cooldown_seconds`.
- Full suite: `python3 -m unittest discover -s tools/agent_pipeline/tests`
  (208 tests, all green). `test_real_pipeline.py`'s `RealPipelineTests.setUp`
  now also monkeypatches `controller.USAGE_ROOT` (alongside the existing
  `TASKS_ROOT` monkeypatch) — any new test added there that exercises a real
  `pipeline_run` must rely on this, not assume it needs to redo it, and must
  never remove it (removing it is exactly how this phase's own draft work
  briefly leaked 82 real ledger entries into the actual repo's
  `.agent-pipeline/usage/` before the monkeypatch was added — cleaned up
  before commit, but worth knowing the failure mode is real and easy to hit
  if a future test adds a new `controller`-module-global root without
  patching it the same way).
