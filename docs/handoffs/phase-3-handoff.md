# Phase 3 handoff — Automated overseer-driven review & decision (Stages 6-8)

**Status:** done (2026-08-05)

## What changed

- **`.agent-pipeline/fixtures/mock_scenarios.json`**: fixed 4 stale fixture
  expectations (`complete`'s `expected_agent_call_counts`;
  `continuity_degraded_review`/`rate_limit_with_reset_fallback`/
  `usage_limit_fallback`'s `expected_exit`/`expected_state`) that predated
  `policies.py`'s provider-fallback and continuity-mode degraded-review
  logic — see "Why" below. `pipeline-mock-test`/`controller.mock_test()` now
  pass cleanly.
- **`verification.py`**: `check_concurrency_guard(task_dir, allow_pid=None)`
  — skips the live-lock refusal when the lock's recorded PID equals
  `allow_pid`, so `run_real_pipeline` can call `run_verification` from
  inside its own already-held `TaskLock` without the guard mistaking itself
  for a foreign concurrent process. `run_verification(..., allow_pid=None)`
  threads it through. The standalone `pipeline-verify` CLI path
  (`controller.pipeline_verify`) still calls it with `allow_pid=None`,
  preserving its guard exactly.
- **`overseer.py`**: `ALLOWED_ROUTES` gained `"auto_verified"`. New
  `upgrade_to_auto_verified(handoff, verification_report)` — pure function,
  forces `route` to `"auto_verified"` and appends a
  no-human-tested-this-in-game note to `known_limitations`, but refuses to
  touch an existing `blocked`/`administrator_action` route (an explicit
  block always outranks evidence that merely looks clean). New
  `verification_summary_bullets(verification_report)` helper, used by both
  `upgrade_to_auto_verified` and `fallback_handoff` (which gained an
  optional `verification_report` parameter) so the deterministic-fallback
  handoff path is no longer stuck saying "No automatic verification was
  marked passed" once verification evidence actually exists.
- **`config.py`**: `DEFAULT_CONFIG["roles"]["07"]` (`primary: claude,
  fallbacks: [agy], independent_from: 05`, mirroring the mock driver's
  long-standing `policies.ROLE_POLICY["07"]`), `turn_budgets["07"]`, new
  top-level `"enable_auto_verified": True` (rollback switch, on by default),
  `validate_config`'s required-role loop extended to `"07"`.
- **`prompts.py`**: new `"07"` branch in `prompt_text` (was previously
  `raise ValueError` for it) — mirrors the legacy `PROMPT_STAGE7` text
  (`Makefile:780-821`): reads the final brief/gate/implementation
  report/manual test notes, points at `05_implementation_manifest.json`'s
  changed-files list and instructs the agent to inspect the repo's own
  `git diff`, independent-reviewer stance, output format matching
  `CONTRACTS["07"]`. `COMPAT_PROMPTS["07"] = ".prompt_stage7_review.txt"`
  (matches the legacy prompt-file naming convention). `"overseer"` prompt
  gained one line pointing at `05_verification_report.md` if present.
- **`artifacts.py`**: new `manual_test_decision(text)` — classifies a Stage
  6 (or Stage 8) outcome as `"accept"`/`"reject"`/`"needs_followup"`: first
  checks for a single checked box in the `Decision`/`Overall manual result`
  section (same regex `validate_manual_test_outcome` already uses), else
  falls back to prose-keyword classification with "worst wins" precedence
  (reject/fail/blocked > needs-follow-up > accept/pass/approved) for the
  free-text path `explicit_manual_outcome` already validated as *present*
  but never classified.
- **`controller.py`** (`run_real_pipeline`, the core of this phase):
  - Right after `write_manifest` succeeds, calls
    `verification.run_verification(task_dir, REPO_ROOT, allow_pid=os.getpid())`,
    swallowing (and logging) `VerificationError` as "no evidence available"
    rather than blocking the run.
  - `run_overseer_or_fallback` gained a `verification_report=None` parameter
    and now **returns** the handoff dict (previously void). After getting
    the handoff (agent-authored or deterministic fallback), it computes
    `auto_verified_eligible` (`enable_auto_verified` config, verification
    `overall_status == "passed"`, coverage signal not `flagged`) and, if
    eligible and the route isn't already `blocked`/`administrator_action`,
    calls `upgrade_to_auto_verified` before writing the handoff files. This
    keeps the safety-relevant decision entirely inside deterministic
    controller code — the LLM overseer never gets to claim `auto_verified`
    for itself.
  - New `render_auto_stage06_notes(verification_report)` — builds a
    contract-conformant `06_manual_test_notes.md` body (checked "Accept",
    an "Automated verification summary" listing each check's status, and
    plain language stating no human tested it in-game).
  - When the handoff's route is `auto_verified`, `run_real_pipeline` writes
    that Stage 6 body via `runner.atomic_finalize` instead of transitioning
    to `awaiting_human_test`, then falls through to drive Stage 07/08 in the
    same call. Otherwise, behavior is byte-for-byte what it was before this
    phase: `awaiting_human_test`, `current_stage: "06"`, `EXIT_BLOCKED`.
  - The entire `stage5_report_provenance`/manifest/overseer/checkpoint-write
    block is now guarded by `"06" not in state.get("completed_stages", [])`
    — so a *resumed* call where Stage 6 is already valid (either just
    auto-written above, or a human finished it since the last run) skips
    straight past it to Stage 07/08 without re-running the overseer agent
    or regenerating the manifest.
  - New `ensure_real_stage(..., "07", "read-only", ...)` call — fully
    generic, reused unmodified; needed only the `config.py`/`prompts.py`
    plumbing above.
  - New `ensure_stage08_decision(task_dir, state)` — controller-local, no
    agent call (matches how Stage 8 was already purely a checkbox doc in
    the legacy pipeline and how the mock driver already treats `"08"` as a
    `LOCAL_STAGES` entry). Reads Stage 6's outcome
    (`manual_test_decision`) and Stage 7's final verdict line, combines them
    with "worst wins" (`accept` < `needs_followup` < `reject`) via
    `worse_decision`, and writes `08_decision.md` (`render_stage08_decision`)
    citing both source verdicts. Idempotent: no-ops and just re-reads the
    already-written decision if `"08"` is already completed.
  - `checkpoint_noop_eligible` gained a check: if `06_manual_test_notes.md`
    has become a *valid* artifact, it now reports **not** eligible for
    no-op — previously its hash set only ever covered artifacts through
    Stage 05, so a human hand-completing Stage 6 had no way to ever unstick
    a real pipeline run (see "Why" below).
  - `pipeline_run`'s process exit code now reflects the Stage 8 outcome:
    `EXIT_SUCCESS` (`0`) for an overall `accept`, `EXIT_VALIDATION` (`1`)
    for `reject`/`needs_followup`. `state` still reaches `"complete"`
    either way — the disposition is legible from `08_decision.md` and the
    exit code, not encoded as a new state value.
  - `status()` now prints a `final_decision: <accept|reject|needs_followup>`
    line once Stage 08 is complete, read back from the file (no new
    `state.json` schema field — deliberately deferred, see below).

## Why

`tools/agent_pipeline/`'s real driver only ever automated Stages 00-05; a
human had to run `stage-6-test-note`/`stage-7-review`/`stage-8-decision` by
hand from the legacy `Makefile`, none of it evidence-aware. Phase 2 built
`verification.py` specifically so Phase 3 would have real build/test
evidence to decide with, and `overseer.py` already had an `auto_verified`
route documented as "Phase 3's job" (see `OVERVIEW.md`'s pre-Phase-3 text).
This phase makes that real: strong verification evidence lets the pipeline
skip the human Stage 6 checkpoint outright, and Stage 7 (an independent
agent review of the diff) and Stage 8 (a decision derived from Stage 6 + 7)
now run automatically either way, closing the "was permanently stuck" gap
`checkpoint_noop_eligible` had (a human finishing Stage 6 by hand had *no*
way to make a subsequent `pipeline-run` do anything at all — its noop check
only ever looked at pre-Stage-6 artifact hashes).

The `mock_pipeline` fixture fix was a prerequisite, not a side project:
without it, `overall_status` would never read `"passed"` for any real task
(one of its three checks would always fail), making `auto_verified`
permanently unreachable regardless of a task's actual diff quality. Tracing
each of the 4 mismatches by hand against `choose_agent`/`handle_failure`/
`mark_unavailable` (confirmed via `python3 -m tools.agent_pipeline.cli
mock-test`) showed all four scenarios' own names describe a *successful*
recovery path (`_fallback`, `continuity_degraded_review`) that the
already-correct `policies.py` fallback/continuity logic produces — the
fixtures' `expected_exit`/`expected_state`/`expected_agent_call_counts`
simply predated that logic being finished and were never updated.

## Design decisions worth knowing

- **The route decision is deterministic, never agent-authored.** The
  overseer LLM only ever proposes `manual_test`/`blocked`/
  `administrator_action`; `auto_verified` is applied *after* the fact by
  `run_overseer_or_fallback`, purely from `verification_report`'s real
  `overall_status`/coverage-signal fields. An agent hallucinating success in
  its handoff JSON cannot skip human testing on its own say-so.
- **Stage 6 auto-skip is on by default** (`enable_auto_verified: true`),
  per explicit user decision this session, with a config rollback switch
  rather than an opt-in default — confirmed acceptable given
  `auto_verified` only fires when build+unit-tests+mock-pipeline all pass
  *and* the coverage-delta signal isn't flagged.
- **`followup-from-review` automation was explicitly deferred** (user
  decision this session) — Stage 7/8 still populate the same legacy-format
  files (`07_diff_review.md`, `08_decision.md`) at the same paths, so the
  existing legacy `make followup-from-review` continues to work unmodified
  against pipeline-produced artifacts.
- **No new `state.json` schema fields.** `reconcile_artifacts` already
  drives `state` to `"complete"` once all 8 artifacts are valid regardless
  of the decision's content (like a CI run "completing" whether or not its
  tests passed); the actual disposition is legible from `08_decision.md`
  and the process exit code, and `status()` now also prints it. Kept
  Phase 3 from needing a `SCHEMA_VERSION` bump.

## How to verify

```
python3 -m unittest discover -s tools/agent_pipeline/tests
# Ran 177 tests ... OK  (was 149 at end of Phase 2)

python3 -m tools.agent_pipeline.cli mock-test
# mock tests passed: 28  (was failing 4/28 before this phase's fixture fix)

make -f Makefile.orchestrator pipeline-mock-test
# same, via the documented entry point
```

Manual end-to-end proof (this session, fake-agent harness, not a real task —
see `tools/agent_pipeline/tests/test_real_pipeline.py` for the equivalent
automated coverage):
- Verification report `{"overall_status": "passed", "test_coverage_delta_signal": {"status": "ok"}}`
  → one `pipeline-run` call drove Stage 00 through 08, `06_manual_test_notes.md`
  auto-written and checked "Accept", `05_supervisor_handoff.json`'s route
  `"auto_verified"`, exit code `0`.
- Verification report `{"overall_status": "incomplete"}` → stopped at
  `awaiting_human_test`/Stage 06 exactly as pre-Phase-3. Hand-wrote a
  checked-"Accept" `06_manual_test_notes.md`, reran `pipeline-run` → drove
  Stage 07/08 automatically this time (previously a permanent no-op), exit
  code `0`.
- Same, with the fake Stage 7 reviewer returning verdict `needs_followup`
  → `08_decision.md` checked "Needs follow-up", exit code `1`
  (`EXIT_VALIDATION`), state still `"complete"`.

Real `pipeline-verify`/`pipeline-run` against a live task directory was not
re-run this session (Phase 2's handoff already has a real end-to-end
`pipeline-verify` proof against `enchanting-ui-tooltip-level-titles`); the
fixture fix makes that same task's next `pipeline-verify` show
`mock_pipeline: passed` instead of `failed`.

## Known gaps

- `followup-from-review` remains legacy-bash-only (see "Design decisions").
- Stage 6 auto-verification never runs real in-game/manual testing — it is
  gated on build/unit-test/coverage-signal evidence only, which can't catch
  GUI/gameplay regressions in a Minecraft mod. `enable_auto_verified: false`
  disables it repo-wide; a human can also overwrite an auto-generated
  `06_manual_test_notes.md` before Stage 7 runs (Stage 7 always runs
  regardless of how Stage 6 was completed).
- Of the ~36 task directories with live `.orchestrator/` state that predate
  this phase: any sitting at `awaiting_human_test` with **no** Stage 6 file
  yet are unaffected (still a clean no-op). Any where a human already
  finished Stage 6 by hand but never got to manually run
  `stage-7-review`/`stage-8-decision` will have Stage 7/8 driven for real
  (a genuine agent subprocess call) on their very next `pipeline-run` —
  intended, but worth knowing before running `pipeline-run` broadly across
  existing tasks.
- No live `.agent-pipeline/config/orchestrator.json` still exists (carried
  over from Phase 2's "Known gaps", not addressed here) — real pipeline
  runs remain silently on `DEFAULT_CONFIG`.

## Deliberately deferred

- `followup-from-review` automation (scaffolding a new correction task from
  a rejected/needs-followup Stage 7 review) — explicit user decision this
  session; not scheduled to a specific future phase.
- Any enforcement stronger than the existing coverage-delta *signal* — a
  `flagged` coverage signal still only blocks `auto_verified` eligibility,
  it does not fail the pipeline outright or force a Stage 7 rejection.
- Model/agent routing intelligence and usage-budget awareness — Phase 4.
- Reasoning-trace capture / "peer into thinking" — Phase 5.

## Notes for the next session (Phase 4)

- Phase 4 is "smart agent/model routing + usage awareness" — the new Stage
  07 role (`config.py`'s `roles["07"]`) and the `auto_verified`/
  `enable_auto_verified` gating logic in `run_overseer_or_fallback` are the
  places a smarter routing layer would need to plug in without breaking the
  "route decision is deterministic, never agent-authored" invariant this
  phase established.
- `render_auto_stage06_notes`/`render_stage08_decision`/`worse_decision` in
  `controller.py` are plain string-building functions with no agent
  dependency — safe to reuse if a future phase wants a different
  presentation without touching control flow.
- Full suite: `python3 -m unittest discover -s tools/agent_pipeline/tests`
  (177 tests, all green; `test_real_pipeline.py`'s `RealPipelineTests.setUp`
  now monkeypatches `controller.verification.run_verification` to a canned
  `"incomplete"` report by default so the fake-agent-CLI test harness never
  shells out to a real `unittest discover`/`gradle` — reuse that pattern
  (`self.verification_report(overall_status=..., coverage_status=...)`)
  rather than letting a new test hit the real subprocess path.
