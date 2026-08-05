# Phase 2 handoff — Test harness expansion + automated verification reporting

**Status:** done (2026-08-05)

## What changed

- **`real_runner.py`**: extracted the `Popen`/`communicate` core of
  `invoke_agent` into a standalone `run_to_files(argv, stdout_path,
  stderr_path, timeout_seconds, stdin_text=None, cwd=None, env=None)` ->
  `(exit_code, timed_out)`. `invoke_agent`'s observable behavior is
  byte-for-byte unchanged (same files written, same exit-code/timeout
  semantics) — this is a pure extraction so `verification.py` has one
  subprocess-invocation pattern to reuse instead of inventing a fourth one
  (agent CLIs, gradle, unittest, mock-test all now go through the same
  function).
- **New module `tools/agent_pipeline/verification.py`**:
  - `check_concurrency_guard(task_dir)` — reads `.orchestrator/lock.json`
    for *that task*; if the recorded PID is live (`locking.pid_live`),
    raises `VerificationError`. Unreadable lock file also raises (fails
    closed). No lock, or a lock with a dead PID, passes silently.
  - `run_unit_tests` — `python3 -m unittest discover -s
    tools/agent_pipeline/tests`, `run_mock_pipeline` — `python3 -m
    tools.agent_pipeline.cli mock-test`, `run_gradle(repo_root, runs_dir,
    gradle_task, ...)` — `./gradlew --no-daemon <task>` with
    `JAVA_HOME=/usr/lib/jvm/java-8-openjdk-amd64` and
    `GRADLE_USER_HOME=<repo_root>/.gradle-user-home` (matches `AGENTS.md`'s
    documented build commands exactly). Each returns a dict with at least
    `name`/`status` (`passed`/`failed`/`not_attempted`); the two Python
    checks also carry `exit_code`/`timed_out`/`duration_seconds`/
    `command`/`stdout_path`/`stderr_path`. `run_gradle` returns
    `not_attempted` (not an error) if `./gradlew` doesn't exist at
    `repo_root`.
  - `test_coverage_delta_signal(manifest)` — detection-only: pulls
    `changed_files` off a Stage 5 manifest, flags `.py`/`.java` paths that
    changed without any path under `tools/agent_pipeline/tests/` or
    `src/test/` also changing. Never raises, never blocks; `status` is one
    of `no_data` (no manifest / no changed files) / `ok` / `flagged`.
  - `update_manifest_verification(task_dir, manifest, checks_by_name,
    coverage_signal)` — best-effort: fills in
    `05_implementation_manifest.json`'s long-standing `verification:
    {unit_tests, mock_pipeline, diff_check}` placeholders (previously
    always `"not_attempted"`, see `manifest.py:57-63`) with this run's real
    statuses, and appends one entry per check to `verification_evidence`.
    `diff_check` is `"failed"` when the coverage signal is `flagged`, else
    `"passed"`. Only touches `verification`/`verification_evidence` —
    never `changed_files`/`stage5_run` — so it can't desync
    `controller.py`'s `stage5_report_provenance` hash check. No-op
    (returns `None`) if no manifest exists for the task yet.
  - `run_verification(task_dir, repo_root, run_build=False,
    unit_test_timeout=600, mock_pipeline_timeout=120,
    gradle_timeout=1800)` — orchestrates all of the above: guard, then
    `unit_tests` + `mock_pipeline` + `gradle_compileJava` (+
    `gradle_build` if `run_build`), then the coverage signal, then the
    manifest update, then writes the report. Returns the report dict.
- **`controller.pipeline_verify(task, run_build=False)`**: thin wrapper —
  catches `VerificationError` (prints message, returns `EXIT_LOCKED`),
  otherwise prints a human-readable summary and returns `EXIT_SUCCESS` if
  `overall_status == "passed"` else `EXIT_VALIDATION`.
- **`cli.py`** / **`Makefile.orchestrator`**: new `pipeline-verify --task T
  [--build]` / `make -f Makefile.orchestrator pipeline-verify TASK=... [BUILD=1]`.
- **New test files** (all direct, pure-function-first, following the
  existing fake-executable-fixture convention from
  `test_real_runner_streaming.py`):
  - `tests/test_real_runner.py` (26 tests) — `build_argv` per agent
    (codex/claude/agy) × per mode (read-only/workspace-write), `classify`
    (every substring/exit-code branch), `extract_candidate` (existing
    file preserved, plain-text fallback, JSONL extraction).
  - `tests/test_overseer.py` (14 tests) — `parse_overseer_candidate`
    (every `ALLOWED_ROUTES` value, every required-field rejection),
    `fallback_handoff` (route/reason/changed_files, and that its own
    output round-trips through `parse_overseer_candidate`).
  - `tests/test_verification.py` (26 tests) — concurrency guard (no
    lock / live PID / dead PID / unreadable lock), `parse_unittest_summary`
    (OK / FAILED / empty stderr), `test_coverage_delta_signal` (all
    status branches), `update_manifest_verification` (fills statuses,
    appends not replaces evidence, writes to disk, `None`-manifest no-op,
    result still satisfies `manifest.validate_manifest`), `run_gradle`
    against a fake `./gradlew` fixture (argv, env vars, nonzero exit,
    missing gradlew), `run_mock_pipeline` for real against this actual
    repo (see "Known gaps" below — asserts the real, currently-failing
    result), and `run_verification`'s full orchestration (guard / report
    write / manifest update / no-manifest path) against fake
    python+gradlew stand-ins so the suite doesn't pay for a real
    `unittest discover` or `gradle` invocation on every run.

## Why

`manifest.py` has carried a `verification: {unit_tests, mock_pipeline,
diff_check}` dict since before this redesign, permanently stuck at
`"not_attempted"` — nothing ever ran the checks. `overseer.fallback_handoff`
even says so explicitly: `"No automatic verification was marked passed by
the controller."` Phase 3 needs real evidence to decide its planned
`auto_verified` overseer route, so Phase 2's job is purely to produce that
evidence in a legible, reusable, already-tested form — this phase does not
change pipeline control flow at all (`verification.py` is never called from
`run_real_pipeline`).

## Verification report JSON schema

`<task_dir>/05_verification_report.json` (Phase 3 depends on this shape):

```json
{
  "schema_version": 1,
  "generated_at": "2026-08-05T15:02:35Z",
  "task": "<task-dir-basename>",
  "manifest_present": true,
  "overall_status": "passed | failed | incomplete",
  "checks": [
    {
      "name": "unit_tests | mock_pipeline | gradle_compileJava | gradle_build",
      "status": "passed | failed | not_attempted",
      "exit_code": 0,
      "timed_out": false,
      "duration_seconds": 2.8,
      "command": ["python3", "-m", "unittest", "..."],
      "stdout_path": ".../verification_runs/unit_tests-....stdout",
      "stderr_path": ".../verification_runs/unit_tests-....stderr",
      "summary": {"tests_run": 149, "ok": true, "failures": 0, "errors": 0}
    }
  ],
  "test_coverage_delta_signal": {
    "status": "no_data | ok | flagged",
    "touched_test_files": false,
    "testable_changed_paths": ["src/main/java/.../Foo.java"],
    "flagged_paths": ["src/main/java/.../Foo.java"],
    "note": "human-readable explanation"
  }
}
```

(`summary` is only present on the `unit_tests` check; `gradle_*` checks
omit `exit_code`/etc. entirely and are `{"name", "status": "not_attempted",
"reason": "..."}` when `./gradlew` is missing.) `overall_status` is
`"failed"` if any *attempted* check failed, `"passed"` if all attempted
checks passed, `"incomplete"` otherwise (e.g. everything `not_attempted`).
A sibling `05_verification_report.md` renders the same data for humans.
`run_verification`'s return value additionally carries `report_paths`
(`{json_path, md_path}`) and `manifest_updated` (bool) that aren't
persisted to the JSON file itself.

## How to verify

```
python3 -m unittest discover -s tools/agent_pipeline/tests
# Ran 149 tests ... OK  (was 83 at end of Phase 1)

# Real end-to-end proof (this session, against a real existing task with
# state=awaiting_human_test, i.e. safe -- no lock, Stage 5 already done):
make -f Makefile.orchestrator pipeline-verify TASK=enchanting-ui-tooltip-level-titles
# task: enchanting-ui-tooltip-level-titles
# overall_status: failed
#   unit_tests: passed (exit=0, 2.8s)
#   mock_pipeline: failed (exit=1, 0.2s)        <- see "Known gaps"
#   gradle_compileJava: passed (exit=0, 7.4s)
# test_coverage_delta_signal: flagged
#   flagged: src/main/.../GuiImmersiveEnchanting.java
# report: .../05_verification_report.md
```

That run wrote `05_verification_report.{json,md}` into the real task
directory and filled in its `05_implementation_manifest.json`'s
`verification` block (was all `"not_attempted"`, now
`{"unit_tests": "passed", "mock_pipeline": "failed", "diff_check":
"failed"}`) — confirmed additive: no other artifact in that task directory
was touched (`.agent-pipeline/` is entirely `.gitignore`d in this repo, so
there's no git diff to inspect, but the file list/mtimes were checked
directly).

## Known gaps

- **`pipeline-mock-test` currently fails against this repo's own
  fixtures** — discovered by this phase's `mock_pipeline` check, not
  caused by it. `complete` gets agent-call counts `{claude: 2, codex: 4}`
  vs. the fixture's expected `{claude: 3, codex: 3}`; `continuity_degraded_review`
  / `rate_limit_with_reset_fallback` / `usage_limit_fallback` all exit `0`
  instead of the fixture's expected `3`. Deterministic across repeated
  runs; confirmed unrelated to any file this phase touched. Root cause is
  presumably drift in `policies.py`/`controller.py`'s fallback logic vs.
  `.agent-pipeline/fixtures/mock_scenarios.json`'s `expected_exit`/
  `expected_agent_call_counts` — not investigated further, since
  root-causing a state-machine regression is out of scope for a
  test-harness phase. **A real `pipeline-verify` run against any task will
  show `mock_pipeline: failed` until a future session fixes this** — that
  is the correct, honest result of the check, not a bug in Phase 2.
- Gradle checks (`compileJava`, `build`) were validated against a fake
  `./gradlew` fixture in the automated suite (fast, deterministic) and
  once for real via the manual smoke test above (`compileJava` only,
  7.4s, this repo's `.gradle-user-home` was already warm). `gradle_build`
  was validated for argv/plumbing only (`--build`/`BUILD=1` flag parses
  correctly end-to-end through `cli.py`) — not run for real this session,
  since a full `build` (vs. `compileJava`) is a materially longer/more
  expensive operation and the plumbing is identical to the already-proven
  `compileJava` path.
- `verification.py` is a pure library + CLI addition; nothing in
  `controller.py`'s `run_real_pipeline` calls it. It has no effect on the
  automatic pipeline flow yet — that's Phase 3's job.

## Deliberately deferred

- Wiring `run_verification` into `run_real_pipeline` / the overseer's
  route decision — Phase 3.
- `verification.py`'s manifest-update assumes `write_manifest` won't run
  again for the same task after it. True today (nothing calls
  `run_verification` automatically), but **if Phase 3 wires this in, call
  it after `write_manifest`'s regeneration, not before** — `write_manifest`
  unconditionally rebuilds `05_implementation_manifest.json` from scratch
  each time it runs and would silently blow away verification data written
  earlier in the same flow.
- No coverage-delta *enforcement* — purely a `flagged`/`ok` signal on the
  report and a `passed`/`failed` value in the manifest's `diff_check` key.
  Turning a `flagged` result into a hard block/required-fix is explicitly
  Phase 3 scope per the original plan.
- No caching/memoization of verification runs — every `pipeline-verify`
  invocation re-runs all checks from scratch. Not requested; `gradle
  --no-daemon` with a warm `.gradle-user-home` was fast enough (7.4s) that
  this wasn't a pain point this session.

## Notes for the next session (Phase 3)

- `05_verification_report.json`'s schema (above) is what Phase 3's
  overseer prompt/parsing should consume when deciding the new
  `auto_verified` route — don't re-derive evidence from raw stdout/stderr
  files, the structured `checks`/`test_coverage_delta_signal` fields
  already exist for this.
- Fix or explicitly accept-and-document the `pipeline-mock-test` failure
  above before leaning on `mock_pipeline: passed` as a real signal for
  auto-routing decisions — right now every task's manifest will show
  `mock_pipeline: failed` regardless of that task's actual diff, which
  would make an overseer route that requires `mock_pipeline: passed`
  permanently unreachable.
- `real_runner.run_to_files` is now the one subprocess-to-files primitive
  in this codebase (agent CLIs, gradle, unittest, mock-test all go through
  it) — reuse it rather than adding a fifth pattern.
- Full suite: `python3 -m unittest discover -s tools/agent_pipeline/tests`
  (149 tests, all green, no Phase 0/1 test needed modification this phase
  either).
