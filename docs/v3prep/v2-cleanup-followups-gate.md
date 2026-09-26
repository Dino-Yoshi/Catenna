# V2 cleanup follow-up gate

Status: **open** — every F01–F05 requirement row below is `PASS` and I1–I5
each have a passing regression, but the tested candidate is an uncommitted
working tree. Per the [follow-up brief](v2-cleanup-followups.md#slice-f05--follow-up-gate)
this gate stays open until the candidate is committed and the required suites
pass at that commit. The [C/R cleanup gate](v2-cleanup-gate.md) is not reopened
by this record. V3 milestone 1 may begin only after both gates are closed.

Tested revision: none yet — the F01–F05 candidate is the uncommitted working tree on `v2-cleanup` over parent `21d83c232ccdb817cd69aedf19f91049ac9cb888` (the operator requested no commit).
Tested on 2026-09-25 with Linux 6.18 and Python 3.14.7. No paid provider was invoked.

## Integration results

Run for the working-tree candidate above, after F05.

| Command | Result | Detail |
| --- | --- | --- |
| `python3 -m unittest discover -s agent_pipeline/tests` | PASS | 765 tests; one optional packaging smoke skipped |
| `python3 -m agent_pipeline.cli mock-test` | PASS | 28 scenarios/checks |

The one skipped test is the optional non-editable packaging smoke; it is not a
required follow-up check and does not cover I1–I5. Any skipped or failed
required entry below changes the gate to open.

## Closing this gate

1. Commit the candidate.
2. At that commit, run both commands above and confirm both pass, with only the
   optional packaging smoke skipped.
3. Replace the `Tested revision:` line with the full 40-character commit hash
   (for example ``Tested revision: `<hash>`.``), update the date and results,
   and change the status to `closed`.

`test_f05_fr2_status_matches_the_closure_rule` rejects a `closed` status unless
every F row is `PASS`, I1–I5 are `CLOSED`, both suites are recorded passing,
and the tested revision is a commit rather than a working tree.

## Audit findings

Source: the 2026-09-25 consistency audit of R01–R08 against `21d83c2`. The
temporary audit probes (`probe_upstream.py`, `probe_writer_budget.py`,
`probe_edited_review.py`) are converted by F05-FR1 into permanent tests with
inverted expectations in `agent_pipeline/tests/test_f_followups.py`. They
replay each probe against the real temporary Git worktree fixture
(`RemediationProbeFixture`) with fake agent CLIs only. The temporary scripts
are not gate evidence.

| Finding | Severity | Observed behavior at `21d83c2` | Owning slice | Permanent regression | Status |
| --- | --- | --- | --- | --- | --- |
| I1 | High | After acceptance, an edit to `02_*` or `03_*` left `current_acceptance: yes` while `current_stage` was `03` and `run` re-dispatched Stage 03. | F01 | `agent_pipeline.tests.test_f_followups.I1UpstreamCascadeProbeTests.test_i1_edit_02_after_acceptance_is_not_current_and_redispatches_03`; `agent_pipeline.tests.test_f_followups.I1UpstreamCascadeProbeTests.test_i1_edit_03_after_acceptance_is_not_current_and_redispatches_04`; `agent_pipeline.tests.test_f_followups.I1UpstreamCascadeProbeTests.test_i1_edit_00_after_acceptance_still_redispatches_02` | CLOSED |
| I2 | High | A failed Stage 5 writer that changed source on its last in-budget attempt still created an approval; once approved it was consumed and the task blocked on "attempt budget exhausted" without dispatching. | F03 | `agent_pipeline.tests.test_f_followups.I2FailedWriterExhaustionProbeTests.test_i2_budget1_failed_writer_creates_no_approval_and_names_path`; `agent_pipeline.tests.test_f_followups.I2FailedWriterExhaustionProbeTests.test_i2_budget2_failed_writer_still_requires_approval_then_second_writer` | CLOSED |
| I3 | Medium | An exhausted Stage 7 review-input identity blocked with "use approve-retry", but no pending approval existed. | F03 | `agent_pipeline.tests.test_f_followups.I3ReviewExhaustionProbeTests.test_i3_exhausted_review_identity_never_suggests_approve_retry` | CLOSED |
| I4 | High | Invalidation left a hand-edited `07_diff_review.md` promotion unmarked; recovery re-promoted the original review over the edit. | F04 | `agent_pipeline.tests.test_f_followups.I4HandEditedReviewProbeTests.test_i4_budget1_hand_edited_review_is_retired_and_not_restored`; `agent_pipeline.tests.test_f_followups.I4HandEditedReviewProbeTests.test_i4_budget2_hand_edited_review_gets_a_new_review` | CLOSED |
| I5 | Medium | The R01 fresh-input allowance lived only in `state.json` and was lost after one failed attempt; a resume fell back to the lifetime attempt counter. | F02 | `agent_pipeline.tests.test_f_followups.I5StageInputBudgetProbeTests.test_i5_budget2_allowance_survives_a_failed_attempt_and_resume`; `agent_pipeline.tests.test_f_followups.I5StageInputBudgetProbeTests.test_i5_budget1_resume_grants_nothing_until_inputs_change` | CLOSED |

Low findings from the same audit are recorded in
[2026-09-25-r01-r08-low-findings.md](../audits/2026-09-25-r01-r08-low-findings.md)
and are out of scope. Low findings from the post-implementation audit of
F01–F05 (I12–I16) are recorded and deferred in
[2026-09-25-f01-f05-low-findings.md](../audits/2026-09-25-f01-f05-low-findings.md);
none blocks this gate. Audit I11 (per-path fingerprints growing `state.json`)
is deferred by operator decision and recorded as an input to
[V3 slice V01](v3.md#slice-v01--explicit-project-context-and-validated-records).

## Requirement evidence

`PASS` means the cited test passed in the discovery run above. The gate test
`test_f05_fr2_every_cited_test_passes` also runs every cited regression and
fails on any failure or skip. No row is satisfied by inspection alone. The
automatic-acceptance guard (A1) is re-checked against the F01–F04 changes by
`test_f05_a1_invalid_evidence_stays_ineligible_and_valid_path_accepts` and by
the per-slice `test_f0N_a1_*` guards.

| Requirement | Evidence | Status |
| --- | --- | --- |
| F01-FR1 | `agent_pipeline.tests.test_f01_upstream_cascade.F01UpstreamCascadeTests.test_f01_fr1_fr3_fr4_editing_any_upstream_artifact_revokes_current_acceptance`; `agent_pipeline.tests.test_f01_upstream_cascade.F01UpstreamCascadeTests.test_f01_fr1_missing_upstream_artifact_revokes_current_acceptance`; `agent_pipeline.tests.test_f01_upstream_cascade.F01UpstreamCascadeTests.test_f01_fr1_invalid_upstream_artifact_revokes_current_acceptance`; `agent_pipeline.tests.test_f01_upstream_cascade.F01UpstreamCascadeTests.test_f01_fr1_unacknowledged_upstream_artifact_is_not_current`; `agent_pipeline.tests.test_f_followups.I1UpstreamCascadeProbeTests.test_i1_edit_02_after_acceptance_is_not_current_and_redispatches_03`; `agent_pipeline.tests.test_f_followups.I1UpstreamCascadeProbeTests.test_i1_edit_03_after_acceptance_is_not_current_and_redispatches_04` | PASS |
| F01-FR2 | `agent_pipeline.tests.test_f01_upstream_cascade.F01UpstreamCascadeTests.test_f01_fr2_staleness_uses_the_reconcile_rule`; `agent_pipeline.tests.test_f01_upstream_cascade.F01UpstreamCascadeTests.test_f01_fr2_evaluate_and_reconcile_share_one_helper` | PASS |
| F01-FR3 | `agent_pipeline.tests.test_f01_upstream_cascade.F01UpstreamCascadeTests.test_f01_fr1_fr3_fr4_editing_any_upstream_artifact_revokes_current_acceptance`; `agent_pipeline.tests.test_f01_upstream_cascade.F01UpstreamCascadeTests.test_f01_edit_02_after_acceptance_redispatches_03_and_restores_acceptance`; `agent_pipeline.tests.test_f01_upstream_cascade.F01UpstreamCascadeTests.test_f01_edit_03_after_acceptance_redispatches_04_and_restores_acceptance`; `agent_pipeline.tests.test_f_followups.I1UpstreamCascadeProbeTests.test_i1_edit_02_after_acceptance_is_not_current_and_redispatches_03`; `agent_pipeline.tests.test_f_followups.I1UpstreamCascadeProbeTests.test_i1_edit_03_after_acceptance_is_not_current_and_redispatches_04`; `agent_pipeline.tests.test_f_followups.I1UpstreamCascadeProbeTests.test_i1_edit_00_after_acceptance_still_redispatches_02` | PASS |
| F01-FR4 | `agent_pipeline.tests.test_f01_upstream_cascade.F01UpstreamCascadeTests.test_f01_fr1_fr3_fr4_editing_any_upstream_artifact_revokes_current_acceptance`; `agent_pipeline.tests.test_f01_upstream_cascade.F01UpstreamCascadeTests.test_f01_fr4_acceptance_boundary_blocks_on_upstream_change`; `agent_pipeline.tests.test_f01_upstream_cascade.F01UpstreamCascadeTests.test_f01_unedited_accepted_task_stays_current_everywhere` | PASS |
| F02-FR1 | `agent_pipeline.tests.test_f02_stage_input_budget.F02IdentityRecordTests.test_f02_fr1_every_budgeted_dispatch_records_its_stage_input_identity`; `agent_pipeline.tests.test_f02_stage_input_budget.F02IdentityRecordTests.test_f02_fr1_identity_binds_stage_key_and_consumed_hashes` | PASS |
| F02-FR2 | `agent_pipeline.tests.test_f02_stage_input_budget.F02BudgetTests.test_f02_fr2_budget1_upstream_edit_grants_exactly_one_dispatch_per_identity`; `agent_pipeline.tests.test_f02_stage_input_budget.F02BudgetTests.test_f02_fr2_budget2_failed_attempt_leaves_one_more_for_the_same_identity`; `agent_pipeline.tests.test_f02_stage_input_budget.F02BudgetTests.test_f02_fr2_unchanged_inputs_never_exceed_the_budget`; `agent_pipeline.tests.test_f02_stage_input_budget.F02BudgetTests.test_f02_fr2_crash_after_dispatch_does_not_refund_the_attempt`; `agent_pipeline.tests.test_f02_stage_input_budget.F02BudgetTests.test_f02_fr2_completion_retries_are_not_charged_to_the_identity`; `agent_pipeline.tests.test_f_followups.I5StageInputBudgetProbeTests.test_i5_budget2_allowance_survives_a_failed_attempt_and_resume`; `agent_pipeline.tests.test_f_followups.I5StageInputBudgetProbeTests.test_i5_budget1_resume_grants_nothing_until_inputs_change` | PASS |
| F02-FR3 | `agent_pipeline.tests.test_f02_stage_input_budget.F02MarkerRemovalTests.test_f02_fr3_legacy_fresh_marker_grants_nothing_and_is_dropped`; `agent_pipeline.tests.test_f02_stage_input_budget.F02MarkerRemovalTests.test_f02_fr3_recover_writes_no_allowance_marker`; `agent_pipeline.tests.test_f02_stage_input_budget.F02MarkerRemovalTests.test_f02_fr3_lifetime_counter_is_numbering_only` | PASS |
| F02-FR4 | `agent_pipeline.tests.test_f02_stage_input_budget.F02VisibilityTests.test_f02_fr4_exhaustion_block_names_stage_identity_in_status_and_report`; `agent_pipeline.tests.test_f02_stage_input_budget.F02VisibilityTests.test_f02_fr4_unblocked_task_shows_no_stage_budget` | PASS |
| F02-FR5 | `agent_pipeline.tests.test_f02_stage_input_budget.F02LegacyProvenanceTests.test_f02_fr5_dispatch_without_consumed_hashes_counts_toward_no_identity`; `agent_pipeline.tests.test_f02_stage_input_budget.F02LegacyProvenanceTests.test_f02_fr5_r01_fr5_block_on_adopting_legacy_success_is_unchanged` | PASS |
| F03-FR1 | `agent_pipeline.tests.test_f03_no_dead_end_approval.F03LiveExhaustionTests.test_f03_fr1_budget1_failed_writer_blocks_without_approval_and_names_path`; `agent_pipeline.tests.test_f03_no_dead_end_approval.F03LiveExhaustionTests.test_f03_fr1_uncertain_source_comparison_names_the_uncertainty`; `agent_pipeline.tests.test_f03_no_dead_end_approval.F03RecoveryTests.test_f03_fr1_recovery_does_not_recreate_approval_without_allowance`; `agent_pipeline.tests.test_f_followups.I2FailedWriterExhaustionProbeTests.test_i2_budget1_failed_writer_creates_no_approval_and_names_path` | PASS |
| F03-FR2 | `agent_pipeline.tests.test_f03_no_dead_end_approval.F03RecoveryTests.test_f03_fr2_new_identity_recreates_source_bound_approval_before_any_writer`; `agent_pipeline.tests.test_f03_no_dead_end_approval.F03RecoveryTests.test_f03_fr2_end_to_end_brief_change_requires_approval_before_writer` | PASS |
| F03-FR3 | `agent_pipeline.tests.test_f03_no_dead_end_approval.F03ConsumptionTests.test_f03_fr3_approval_stays_unconsumed_when_nothing_is_dispatched`; `agent_pipeline.tests.test_f03_no_dead_end_approval.F03ConsumptionTests.test_f03_fr3_exhausted_source_bound_approval_is_not_consumed`; `agent_pipeline.tests.test_f03_no_dead_end_approval.F03LiveExhaustionTests.test_f03_budget2_first_failure_still_leads_to_approval_and_a_second_writer`; `agent_pipeline.tests.test_f_followups.I2FailedWriterExhaustionProbeTests.test_i2_budget2_failed_writer_still_requires_approval_then_second_writer` | PASS |
| F03-FR4 | `agent_pipeline.tests.test_f03_no_dead_end_approval.F03WithdrawalTests.test_f03_fr4_unapproved_dead_approval_is_withdrawn_durably_on_run`; `agent_pipeline.tests.test_f03_no_dead_end_approval.F03WithdrawalTests.test_f03_fr4_approved_dead_approval_is_withdrawn_durably_on_run`; `agent_pipeline.tests.test_f03_no_dead_end_approval.F03WithdrawalTests.test_f03_fr4_approve_retry_withdraws_an_approval_with_no_attempt_left` | PASS |
| F03-FR5 | `agent_pipeline.tests.test_f03_no_dead_end_approval.F03Stage7ReasonTests.test_f03_fr5_stage7_exhaustion_names_identity_and_never_suggests_approve_retry`; `agent_pipeline.tests.test_f_followups.I3ReviewExhaustionProbeTests.test_i3_exhausted_review_identity_never_suggests_approve_retry` | PASS |
| F04-FR1 | `agent_pipeline.tests.test_f04_invalidation_retires_promotions.F04InvalidationRecordTests.test_f04_fr1_fr2_matching_canonical_is_retired_without_mismatch`; `agent_pipeline.tests.test_f04_invalidation_retires_promotions.F04InvalidationRecordTests.test_f04_fr1_fr2_hand_edited_canonical_is_retired_and_flags_mismatch`; `agent_pipeline.tests.test_f04_invalidation_retires_promotions.F04InvalidationRecordTests.test_f04_fr1_fr2_missing_canonical_is_retired_with_null_archived_hash`; `agent_pipeline.tests.test_f04_invalidation_retires_promotions.F04InvalidationRecordTests.test_f04_fr1_fr2_nothing_archived_still_retires_promotion`; `agent_pipeline.tests.test_f04_invalidation_retires_promotions.F04InvalidationRecordTests.test_f04_fr1_sidecar_is_written_before_any_canonical_file_is_removed`; `agent_pipeline.tests.test_f04_invalidation_retires_promotions.F04InvalidationRecordTests.test_f04_fr1_every_unretired_promotion_is_retired_and_existing_records_are_immutable`; `agent_pipeline.tests.test_f04_invalidation_retires_promotions.F04InvalidationRecordTests.test_f04_fr1_other_stages_are_untouched` | PASS |
| F04-FR2 | `agent_pipeline.tests.test_f04_invalidation_retires_promotions.F04InvalidationRecordTests.test_f04_fr1_fr2_matching_canonical_is_retired_without_mismatch`; `agent_pipeline.tests.test_f04_invalidation_retires_promotions.F04InvalidationRecordTests.test_f04_fr1_fr2_hand_edited_canonical_is_retired_and_flags_mismatch`; `agent_pipeline.tests.test_f04_invalidation_retires_promotions.F04InvalidationRecordTests.test_f04_fr1_fr2_missing_canonical_is_retired_with_null_archived_hash`; `agent_pipeline.tests.test_f04_invalidation_retires_promotions.F04InvalidationRecordTests.test_f04_fr1_fr2_nothing_archived_still_retires_promotion` | PASS |
| F04-FR3 | `agent_pipeline.tests.test_f04_invalidation_retires_promotions.F04RecoveryTests.test_f04_fr3_recovery_never_restores_invalidated_hand_edited_review`; `agent_pipeline.tests.test_f04_invalidation_retires_promotions.F04RecoveryTests.test_f04_c08_fr3_crash_after_promotion_is_still_adopted_once`; `agent_pipeline.tests.test_f04_invalidation_retires_promotions.F04RecoveryTests.test_f04_c08_fr3_unpromoted_success_after_invalidation_is_still_adopted`; `agent_pipeline.tests.test_f04_invalidation_retires_promotions.F04EndToEndTests.test_f04_acceptance_budget1_hand_edit_is_not_reverted_and_blocks_on_exhaustion`; `agent_pipeline.tests.test_f04_invalidation_retires_promotions.F04EndToEndTests.test_f04_acceptance_budget2_hand_edit_produces_a_new_review`; `agent_pipeline.tests.test_f_followups.I4HandEditedReviewProbeTests.test_i4_budget1_hand_edited_review_is_retired_and_not_restored`; `agent_pipeline.tests.test_f_followups.I4HandEditedReviewProbeTests.test_i4_budget2_hand_edited_review_gets_a_new_review` | PASS |
| F05-FR1 | `agent_pipeline.tests.test_f_followups.F05FollowupGateRecordTests.test_f05_fr1_each_finding_has_a_converted_probe_regression`; `agent_pipeline.tests.test_f_followups.I1UpstreamCascadeProbeTests.test_i1_edit_02_after_acceptance_is_not_current_and_redispatches_03`; `agent_pipeline.tests.test_f_followups.I2FailedWriterExhaustionProbeTests.test_i2_budget1_failed_writer_creates_no_approval_and_names_path`; `agent_pipeline.tests.test_f_followups.I3ReviewExhaustionProbeTests.test_i3_exhausted_review_identity_never_suggests_approve_retry`; `agent_pipeline.tests.test_f_followups.I4HandEditedReviewProbeTests.test_i4_budget1_hand_edited_review_is_retired_and_not_restored`; `agent_pipeline.tests.test_f_followups.I5StageInputBudgetProbeTests.test_i5_budget2_allowance_survives_a_failed_attempt_and_resume` | PASS |
| F05-FR2 | `agent_pipeline.tests.test_f_followups.F05FollowupGateRecordTests.test_f05_fr2_record_names_revision_date_environment_and_integration_results`; `agent_pipeline.tests.test_f_followups.F05FollowupGateRecordTests.test_f05_fr2_every_f_requirement_row_is_pass_with_loadable_evidence`; `agent_pipeline.tests.test_f_followups.F05FollowupGateRecordTests.test_f05_fr2_every_cited_test_passes`; `agent_pipeline.tests.test_f_followups.F05FollowupGateRecordTests.test_f05_fr2_status_matches_the_closure_rule`; `agent_pipeline.tests.test_f_followups.F05FollowupGateRecordTests.test_f05_fr2_cr_gate_record_stays_closed`; `agent_pipeline.tests.test_f_followups.F05AutomaticAcceptanceGuardTests.test_f05_a1_invalid_evidence_stays_ineligible_and_valid_path_accepts` | PASS |
| F05-FR3 | `agent_pipeline.tests.test_f_followups.F05FollowupGateRecordTests.test_f05_fr3_srs_wording_reflects_identity_budgets_and_no_dead_end_approval` | PASS |
| F05-FR4 | `agent_pipeline.tests.test_f_followups.F05FollowupGateRecordTests.test_f05_fr4_usage_documents_identity_budgets_and_exhaustion_without_approval`; `agent_pipeline.tests.test_f_followups.I3ReviewExhaustionProbeTests.test_i3_exhausted_review_identity_never_suggests_approve_retry` | PASS |
| F05-FR5 | `agent_pipeline.tests.test_f_followups.F05FollowupGateRecordTests.test_f05_fr5_status_lines_entry_gate_and_i11_input` | PASS |

## Documentation amended by F05

- [v2-cleanup.md](v2-cleanup.md): C03-FR4 and C06-FR3 describe per-input-identity
  budgets and blocking without an approval at exhaustion; status line refreshed.
- [v2-cleanup-gate.md](v2-cleanup-gate.md): the deferred approve-retry item is
  marked resolved by F03.
- [USAGE.md](../USAGE.md): new "Stage attempt allowance" section; the re-review,
  recovery, state table, and `stage_attempt_budget` config entries match.
- [v2-cleanup-remediation.md](v2-cleanup-remediation.md),
  [srs-review.md](srs-review.md), [v2-cleanup-followups.md](v2-cleanup-followups.md):
  status lines refreshed.
- [v3.md](v3.md): the entry gate requires both the C09 gate and this gate; I11
  is recorded as a V01 input.

## Unresolved issues

- The gate is open only because the candidate is uncommitted (see
  [Closing this gate](#closing-this-gate)).
