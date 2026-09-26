# R01–R08 consistency audit — low-severity findings (2026-09-25)

Audited candidate: `fad62ad` plus the uncommitted C01–C09 and R01–R08 working
tree. Source: the 2026-09-25 consistency audit of
[v2-cleanup-remediation.md](../v3prep/v2-cleanup-remediation.md) against the
code, tests, and [gate record](../v3prep/v2-cleanup-gate.md).

These findings are all low severity. None of them reopens D1–D8, grants
acceptance on stale evidence, or blocks the cleanup gate. They are recorded
here so the gate record stays focused on High and Medium items. The audit made
no runtime changes.

Validation at audit time: `python3 -m unittest discover -s agent_pipeline/tests`
ran 689 tests with one optional packaging smoke skipped, and
`python3 -m agent_pipeline.cli mock-test` passed 28 checks/scenarios. Both
match the gate record.

## Findings

| ID | Area | Finding | Evidence | Suggested action |
| --- | --- | --- | --- | --- |
| I6 | R04 / USAGE | SIGINT/SIGTERM handlers are installed only while a managed child runs (`run_to_files`). A SIGTERM between children kills the controller with no persisted interrupted outcome: state stays `running` and the locks remain, so `unlock` is needed. A SIGINT at that point raises `KeyboardInterrupt` with a traceback and no `block()`. `USAGE.md` ("Interruption exit behavior") describes uniform behavior. | Inspection: `real_runner.py` `_install_interruption_handlers`, called only inside `run_to_files`. Not probed. | Install the handlers for the whole `run`/`verify` invocation, or narrow the `USAGE.md` wording to "while a managed child is running". |
| I7 | R04 / C05 | Handlers remain installed during `_terminate_and_reap`. A second Ctrl-C in the five-second grace period interrupts the reap. The outer `except BaseException` retries once; a third Ctrl-C can escape without reaping. This fails safe: the recorded live child keeps the lock and blocks reuse. | Inspection: `real_runner.py` `run_to_files`. | Ignore or defer further signals while terminating and reaping. |
| I8 | R04 / USAGE | An interrupted standalone `catenna verify` also sets the task state to `blocked` at stage `05`, even on a completed task. `USAGE.md` says only "Under `run`, the task is left `blocked`". | `controller.run_bound_verification` calls `block(state, "05", ...)` in the `ManagedProcessInterrupted` path for every origin. | Either document that `verify` does the same, or record the interruption for `verify` without changing the pipeline state. |
| I9 | R01-FR2 | The live promotion path in `ensure_real_stage` (normal and completion retry), and the Stage 06/08 calls in `run_real_pipeline`, still acknowledge consumed inputs from current files through `state.acknowledge_consumed_inputs`, not from the dispatch-time hashes. The C07 integrity check limits the exposure to the short interval between the postcondition and acknowledgement. | `controller.py`: `acknowledge_consumed_inputs(task_dir, state, stage_key)` after `record_promotion`; `run_real_pipeline` for `"06"` and `"08"`. | Acknowledge `dispatch["consumed_input_hashes"]` via `acknowledge_recorded_consumed_inputs` on the live path as well. |
| I10 | R01-FR1 | `attempts.prepare_dispatch` accepts an `input_hashes` parameter but ignores it, and writes the same consumed-input dict under both `input_hashes` and `consumed_input_hashes`. `invoke_stage` still passes `state.get("input_hashes")`. | `attempts.py` `prepare_dispatch`; `controller.invoke_stage`. | Remove the dead parameter; keep one key (retain `input_hashes` only if older readers need it). |
| D1 | Docs | `v2-cleanup-remediation.md` still reads "Status: scoped, not implemented." | `docs/v3prep/v2-cleanup-remediation.md` line 3. | Change to implemented, with the gate closed on 2026-09-25. |
| D3 | Docs | The `srs-review.md` summary table still says "(gate reopened 2026-09-25)". | `docs/v3prep/srs-review.md` line 17. | Change to "gate closed 2026-09-25". |
| D4 | Docs | `OVERVIEW.md` was updated for the C slices but not for R01–R08. It does not mention `attempts.py`, `durable.py`, `integrity.py`, or `source_identity.py`, exit status 130, per-identity Stage 7 budgets, or manual acceptance with configured checks. Its "Retry/fallback" section predates both. R08-FR4 required only `USAGE.md`, so no requirement fails. | `grep` of `docs/OVERVIEW.md`. | Add the modules to "Key modules" and a short note in "Retry/fallback". |
| D6 | Test hygiene | `test_valid_stage5_report_with_partial_postprocessing_blocks` became `test_valid_stage5_report_with_partial_postprocessing_is_recovered`; its expectation changed from `blocked`/`stage5_ambiguity` to `awaiting_human_test`. The brief requires a changed expectation to name its requirement; this test does not. The change matches C08-FR3 adoption. No other test assertions were weakened; the C02 prose-decision removals are intentional. | `git diff -- agent_pipeline/tests/test_real_pipeline.py`. | Add a C08-FR3 reference to the test name or docstring. |

## Out of scope here

High and Medium findings from the same audit (I1–I5, I11, D2, D5) are tracked
separately and are not repeated here.
