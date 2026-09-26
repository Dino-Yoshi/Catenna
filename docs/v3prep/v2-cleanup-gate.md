# V2 cleanup completion gate

Status: **closed** — 2026-09-25, after remediation slices R01–R08 in
[v2-cleanup-remediation.md](v2-cleanup-remediation.md). The gate was reopened
earlier the same day when a review found cross-slice defects D1–D8 (see
[Reopened defects](#reopened-defects)); each now has a permanent passing
regression. This record covers only cleanup and does not start V3 work.

Tested revision: `fad62ad0498c4849dee9d6dca153e334048f7dec` plus the C01–C09
and R01–R08 working-tree candidate (the operator requested no commit). Tested
on 2026-09-25 with Linux 6.18 and Python 3.14.7. No paid provider was invoked.

## Integration results

Closure run for the candidate above, after R08.

| Command | Result | Detail |
| --- | --- | --- |
| `python3 -m unittest discover -s agent_pipeline/tests` | PASS | 689 tests; one optional packaging smoke skipped |
| `python3 -m agent_pipeline.cli mock-test` | PASS | 28 scenarios/checks |

The one skipped test is the optional non-editable packaging smoke; it is not a
required cleanup check and does not cover A1–A8 or D1–D8. Any skipped or failed required
entry below changes the gate to open. The audit reproduction scripts are
historical demonstrations of the old defects and are not gate evidence.

## Audit-finding regressions

| Finding | Owning slice | Permanent regression |
| --- | --- | --- |
| A1 | C01 | `agent_pipeline.tests.test_real_pipeline.RealPipelineTests.test_agent_auto_verified_claim_is_rejected_with_invalid_evidence`; `agent_pipeline.tests.test_c09_cleanup_gate.C09AutomaticAcceptanceRegressionTests.test_c09_fr1_valid_controller_evidence_reaches_automatic_acceptance`; re-checked after remediation by `agent_pipeline.tests.test_r_cleanup_remediation.AutomaticAcceptanceGuardTests.test_a1_invalid_verification_evidence_stays_ineligible`; `agent_pipeline.tests.test_r_cleanup_remediation.AutomaticAcceptanceGuardTests.test_a1_valid_controller_evidence_reaches_automatic_acceptance` |
| A2 | C02 | `agent_pipeline.tests.test_decision.Stage08DecisionTests.test_invalid_stage6_prose_cannot_reach_stage8` |
| A3 | C06 | `agent_pipeline.tests.test_c06_evidence.C06EvidenceTests.test_fr3_audit_a3_new_failing_source_revokes_old_acceptance` |
| A4 | C07 | `agent_pipeline.tests.test_c07_postconditions.C07PostconditionTests.test_fr1_normal_attempt_detects_change_to_already_dirty_file_and_stops_dispatch`; completion and overseer variants are in the same module |
| A5 | C05 | `agent_pipeline.tests.test_real_runner.RunToFilesTests.test_r04_fr1_c05_fr1_fr2_sigint_and_sigterm_propagate_after_persisted_cleanup`; orphan recovery is covered in `test_locking` |
| A6 | C04 | `agent_pipeline.tests.test_locking.WorktreeOwnershipTests.test_c04_fr1_subprocess_write_write_and_write_verify_are_exclusive` |
| A7 | C08 | `agent_pipeline.tests.test_c08_durable_recovery.C08DurableRecoveryTests.test_fr2_unique_attempt_paths_and_recoverable_promotion` |
| A8 | C03 | `agent_pipeline.tests.test_real_pipeline.RealPipelineTests.test_c03_failed_process_outcomes_with_valid_artifacts_are_not_promoted` |

The C04/C05 concurrency and cancellation regressions launch real local
subprocesses. C06 source-fingerprint regressions create and mutate real
temporary Git working trees. The D1–D8 remediation regressions in
`test_r_cleanup_remediation` replay the 2026-09-25 review probes against the
same real-worktree fixture, and D4 launches real local subprocesses.

## Requirement evidence

`PASS` means the cited test passed in the full discovery run above. There are
no inspection-only substitutions in this record; documentation assertions are
also executable regressions. The gate is closed only while every C and R row
below is `PASS` (enforced by `test_c09_fr4` and `test_r08_fr2`).

| Requirement | Evidence | Status |
| --- | --- | --- |
| C01-FR1 | `agent_pipeline.tests.test_overseer.ParseOverseerCandidateTests.test_rejects_agent_provided_auto_verified_route`; `agent_pipeline.tests.test_real_pipeline.RealPipelineTests.test_agent_auto_verified_claim_is_rejected_with_invalid_evidence` | PASS |
| C01-FR2 | `agent_pipeline.tests.test_c09_cleanup_gate.C09AutomaticAcceptanceRegressionTests.test_c09_fr1_invalid_evidence_cannot_authorize_automatic_acceptance`; `agent_pipeline.tests.test_c09_cleanup_gate.C09AutomaticAcceptanceRegressionTests.test_c09_fr1_valid_controller_evidence_reaches_automatic_acceptance` | PASS |
| C01-FR3 | `agent_pipeline.tests.test_real_pipeline.RealPipelineTests.test_stage6_transition_rechecks_malformed_auto_verified_evidence`; `agent_pipeline.tests.test_overseer.ParseOverseerCandidateTests.test_persisted_controller_auto_verified_route_remains_readable` | PASS |
| C02-FR1 | `agent_pipeline.tests.test_artifacts.ArtifactValidationTests.test_stage_6_requires_explicit_manual_outcome`; `agent_pipeline.tests.test_decision.Stage08DecisionTests.test_each_valid_stage6_checkbox_is_consumed` | PASS |
| C02-FR2 | `agent_pipeline.tests.test_artifacts.ArtifactValidationTests.test_stage_6_rejects_duplicate_checked_outcome_lines`; `agent_pipeline.tests.test_artifacts.ArtifactValidationTests.test_stage_6_rejects_duplicate_decision_sections` | PASS |
| C02-FR3 | `agent_pipeline.tests.test_artifacts.ArtifactValidationTests.test_stage_6_prose_only_notes_require_checkbox_correction`; `agent_pipeline.tests.test_decision.Stage08DecisionTests.test_reject_then_needs_followup_then_accept_precedence` | PASS |
| C03-FR1 | `agent_pipeline.tests.test_real_pipeline.RealPipelineTests.test_c03_failed_process_outcomes_with_valid_artifacts_are_not_promoted`; `agent_pipeline.tests.test_real_pipeline.RealPipelineTests.test_c03_successful_results_still_reach_automatic_acceptance` | PASS |
| C03-FR2 | `agent_pipeline.tests.test_real_runner.ClassifyTests.test_zero_exit_does_not_hide_recognized_terminal_error`; `agent_pipeline.tests.test_real_pipeline.RealPipelineTests.test_c03_failed_stage7_accept_cannot_authorize_stage8` | PASS |
| C03-FR3 | `agent_pipeline.tests.test_real_pipeline.RealPipelineTests.test_c03_only_successful_completion_retry_is_promoted_with_own_identity`; `agent_pipeline.tests.test_real_pipeline.RealPipelineTests.test_c03_failed_completion_with_valid_artifact_is_not_promoted` | PASS |
| C03-FR4 | `agent_pipeline.tests.test_real_pipeline.RealPipelineTests.test_c03_failed_writer_change_requires_source_bound_approval_before_retry`; `agent_pipeline.tests.test_real_pipeline.RealPipelineTests.test_c03_failed_writer_with_uncertain_source_blocks_further_writers`; closes D3, D7: `agent_pipeline.tests.test_r_cleanup_remediation.D3D7FailedWriterApprovalTests.test_d3_crash_before_approval_bookkeeping_blocks_second_writer`; `agent_pipeline.tests.test_r_cleanup_remediation.D3D7FailedWriterApprovalTests.test_d7_mode_only_change_to_dirty_file_requires_durable_approval` | PASS |
| C04-FR1 | `agent_pipeline.tests.test_locking.WorktreeOwnershipTests.test_c04_fr1_subprocess_write_write_and_write_verify_are_exclusive` | PASS |
| C04-FR2 | `agent_pipeline.tests.test_locking.WorktreeOwnershipTests.test_c04_fr2_aliases_share_one_key_and_distinct_worktrees_do_not`; `agent_pipeline.tests.test_locking.WorktreeOwnershipTests.test_c04_fr2_controller_paths_and_background_launch_no_child_when_owned` | PASS |
| C04-FR3 | `agent_pipeline.tests.test_locking.WorktreeOwnershipTests.test_c04_fr3_atomic_race_and_consistent_lock_order`; `agent_pipeline.tests.test_locking.WorktreeOwnershipTests.test_c04_fr3_unlock_refuses_live_or_uncertain_worktree_owner` | PASS |
| C05-FR1 | `agent_pipeline.tests.test_real_runner.RunToFilesTests.test_r04_fr1_c05_fr1_fr2_sigint_and_sigterm_propagate_after_persisted_cleanup`; `agent_pipeline.tests.test_real_runner.RunToFilesTests.test_c05_fr1_grace_deadline_forces_group_and_reaps_direct_child`; closes D4: `agent_pipeline.tests.test_r_cleanup_remediation.D4InterruptionTests.test_d4_sigint_in_first_real_check_stops_second_check`; `agent_pipeline.tests.test_r04_interruption.R04RealCheckInterruptionTests.test_r04_fr1_fr2_fr3_sigint_and_sigterm_stop_remaining_checks` | PASS |
| C05-FR2 | `agent_pipeline.tests.test_real_runner.RunToFilesTests.test_r04_fr1_c05_fr1_fr2_sigint_and_sigterm_propagate_after_persisted_cleanup`; `agent_pipeline.tests.test_real_runner.RunToFilesTests.test_on_launch_runs_after_popen_returns`; closes D4: `agent_pipeline.tests.test_r_cleanup_remediation.D4InterruptionTests.test_d4_sigint_in_first_real_check_stops_second_check`; `agent_pipeline.tests.test_r_cleanup_remediation.D4InterruptionTests.test_d4_interrupted_verification_invokes_no_overseer_and_exits_130` | PASS |
| C05-FR3 | `agent_pipeline.tests.test_locking.WorktreeOwnershipTests.test_c05_fr3_abrupt_controller_death_keeps_live_orphan_blocked`; `agent_pipeline.tests.test_locking.WorktreeOwnershipTests.test_c05_fr3_incomplete_child_identity_blocks_and_pid_reuse_is_not_killed` | PASS |
| C06-FR1 | `agent_pipeline.tests.test_source_identity.SourceIdentityFingerprintTests.test_dirty_edit_and_second_edit_of_already_dirty_file`; `agent_pipeline.tests.test_source_identity.SourceIdentityFingerprintTests.test_tracked_symlink_target_is_represented_without_following`; `agent_pipeline.tests.test_c06_evidence.C06EvidenceTests.test_fr1_fr2_valid_automatic_acceptance_records_bound_evidence` | PASS |
| C06-FR2 | `agent_pipeline.tests.test_c06_evidence.C06EvidenceTests.test_fr2_source_change_during_verification_is_not_current`; `agent_pipeline.tests.test_c06_evidence.C06EvidenceTests.test_fr2_manual_notes_must_cite_the_identity_status_exposes`; `agent_pipeline.tests.test_c06_evidence.C06EvidenceTests.test_fr2_source_change_during_review_is_not_bound`; closes D5: `agent_pipeline.tests.test_r_cleanup_remediation.D5ManualAcceptanceTests.test_d5_manual_accept_does_not_survive_newer_failed_check` | PASS |
| C06-FR3 | `agent_pipeline.tests.test_c06_evidence.C06EvidenceTests.test_fr3_source_changes_after_acceptance_require_new_evidence_not_implementation`; `agent_pipeline.tests.test_c06_evidence.C06EvidenceTests.test_fr3_configuration_changes_invalidate_applicable_evidence`; closes D1, D2: `agent_pipeline.tests.test_r_cleanup_remediation.D1ConsumedInputRecoveryTests.test_d1_request_edit_after_acceptance_revokes_and_redispatches`; `agent_pipeline.tests.test_r_cleanup_remediation.D2ReviewInvalidationAndBudgetTests.test_d2_two_review_config_changes_each_produce_one_review_without_conflict` | PASS |
| C06-FR4 | `agent_pipeline.tests.test_c06_evidence.C06EvidenceTests.test_fr4_legacy_decision_without_identities_is_historical_not_current` | PASS |
| C06-FR5 | `agent_pipeline.tests.test_c06_evidence.C06EvidenceTests.test_fr5_internally_inconsistent_recorded_identity_is_not_current`; `agent_pipeline.tests.test_c06_evidence.C06EvidenceTests.test_fr5_later_failed_verification_supersedes_older_pass`; `agent_pipeline.tests.test_c06_evidence.C06EvidenceTests.test_fr5_interrupted_verification_supersedes_older_pass`; closes D8: `agent_pipeline.tests.test_r_cleanup_remediation.D8SnapshotConsistencyTests.test_d8_persistent_mid_capture_edit_raises_instead_of_mixed_identity`; `agent_pipeline.tests.test_r_cleanup_remediation.D8SnapshotConsistencyTests.test_d8_single_mid_capture_edit_retries_to_the_final_content` | PASS |
| C07-FR1 | `agent_pipeline.tests.test_c07_postconditions.C07PostconditionTests.test_fr1_normal_attempt_detects_change_to_already_dirty_file_and_stops_dispatch`; `agent_pipeline.tests.test_c07_postconditions.C07PostconditionTests.test_fr1_completion_retry_uses_same_guard_and_cannot_promote`; `agent_pipeline.tests.test_c07_postconditions.C07PostconditionTests.test_fr1_overseer_mutation_blocks_without_fallback_or_automatic_acceptance` | PASS |
| C07-FR2 | `agent_pipeline.tests.test_c07_postconditions.C07PostconditionTests.test_fr2_unavailable_source_comparison_blocks_promotion_and_retry` | PASS |
| C07-FR3 | `agent_pipeline.tests.test_c07_postconditions.C07PostconditionTests.test_fr3_unsupported_read_only_adapter_is_not_launched` | PASS |
| C07-FR4 | `agent_pipeline.tests.test_c07_postconditions.C07PostconditionTests.test_fr4_implementation_cannot_tamper_with_controller_state`; `agent_pipeline.tests.test_c07_postconditions.C07PostconditionTests.test_fr4_review_cannot_tamper_with_approved_input_or_policy`; closes D6: `agent_pipeline.tests.test_r_cleanup_remediation.D6OwnershipRecordTests.test_d6_deleted_worktree_or_task_lock_fails_postcondition`; `agent_pipeline.tests.test_r_cleanup_remediation.D6OwnershipRecordTests.test_d6_agent_deleting_worktree_lock_blocks_promotion_and_dispatch` | PASS |
| C08-FR1 | `agent_pipeline.tests.test_c08_durable_recovery.C08DurableRecoveryTests.test_fr1_failed_pre_dispatch_persistence_launches_no_child` | PASS |
| C08-FR2 | `agent_pipeline.tests.test_c08_durable_recovery.C08DurableRecoveryTests.test_fr2_unique_attempt_paths_and_recoverable_promotion` | PASS |
| C08-FR3 | `agent_pipeline.tests.test_c08_durable_recovery.C08DurableRecoveryTests.test_fr3_invalid_evidence_blocks_but_valid_success_is_adopted_once`; closes D1, D2: `agent_pipeline.tests.test_r_cleanup_remediation.D1ConsumedInputRecoveryTests.test_d1_recovery_does_not_rewrite_acknowledged_request_hash`; `agent_pipeline.tests.test_r_cleanup_remediation.D2ReviewInvalidationAndBudgetTests.test_d2_recovery_does_not_resurrect_invalidated_review` | PASS |
| C08-FR4 | `agent_pipeline.tests.test_c08_durable_recovery.C08DurableRecoveryTests.test_fr4_recovery_retains_attempt_and_consumed_approval`; closes D2, D3: `agent_pipeline.tests.test_r_cleanup_remediation.D2ReviewInvalidationAndBudgetTests.test_d2_failing_rereview_is_dispatched_once_despite_repeated_resume`; `agent_pipeline.tests.test_r_cleanup_remediation.D3D7FailedWriterApprovalTests.test_d3_crash_before_approval_bookkeeping_blocks_second_writer` | PASS |
| C08-FR5 | `agent_pipeline.tests.test_c08_durable_recovery.C08DurableRecoveryTests.test_fr5_replace_failure_retains_state_and_launch_record_failure_reaps_child` | PASS |
| C09-FR1 | `agent_pipeline.tests.test_c09_cleanup_gate.C09CleanupGateRecordTests.test_c09_fr1_each_finding_has_a_permanent_guard_using_required_fixtures`; `agent_pipeline.tests.test_c09_cleanup_gate.C09AutomaticAcceptanceRegressionTests.test_c09_fr1_invalid_evidence_cannot_authorize_automatic_acceptance`; `agent_pipeline.tests.test_c09_cleanup_gate.C09AutomaticAcceptanceRegressionTests.test_c09_fr1_valid_controller_evidence_reaches_automatic_acceptance`; closes D1–D8: `agent_pipeline.tests.test_c09_cleanup_gate.C09CleanupGateRecordTests.test_r08_fr1_fr3_every_defect_is_closed_by_a_passing_remediation_regression` | PASS |
| C09-FR2 | `agent_pipeline.tests.test_c09_cleanup_gate.C09CleanupGateRecordTests.test_c09_fr2_required_commands_are_recorded_passing` | PASS |
| C09-FR3 | `agent_pipeline.tests.test_c09_cleanup_gate.C09CleanupGateRecordTests.test_c09_fr3_operator_documentation_covers_corrected_guarantees` | PASS |
| C09-FR4 | `agent_pipeline.tests.test_c09_cleanup_gate.C09CleanupGateRecordTests.test_c09_fr4_every_requirement_maps_to_loadable_passing_evidence`; `agent_pipeline.tests.test_c09_cleanup_gate.C09CleanupGateRecordTests.test_r08_fr2_gate_requires_every_c_and_r_row_to_pass` | PASS |
| R01-FR1 | `agent_pipeline.tests.test_r01_recovery.R01RecoveryTests.test_r01_fr1_dispatch_hashes_live_consumed_artifacts`; `agent_pipeline.tests.test_r01_recovery.R01RecoveryTests.test_r01_fr1_fr2_request_edit_revokes_acceptance_and_redispatches_stage02` | PASS |
| R01-FR2 | `agent_pipeline.tests.test_r01_recovery.R01RecoveryTests.test_r01_fr2_changed_input_is_stale_and_not_acknowledged`; `agent_pipeline.tests.test_r01_recovery.R01RecoveryTests.test_r01_fr2_valid_success_is_adopted_once_and_auto_acceptance_still_works`; `agent_pipeline.tests.test_r_cleanup_remediation.D1ConsumedInputRecoveryTests.test_d1_recovery_does_not_rewrite_acknowledged_request_hash` | PASS |
| R01-FR3 | `agent_pipeline.tests.test_r01_recovery.R01RecoveryTests.test_r01_fr3_invalidated_promotion_is_never_resurrected`; `agent_pipeline.tests.test_r_cleanup_remediation.D2ReviewInvalidationAndBudgetTests.test_d2_recovery_does_not_resurrect_invalidated_review` | PASS |
| R01-FR4 | `agent_pipeline.tests.test_r01_recovery.R01RecoveryTests.test_r01_fr4_later_promotion_supersedes_earlier_success`; `agent_pipeline.tests.test_r_cleanup_remediation.D2ReviewInvalidationAndBudgetTests.test_d2_two_review_config_changes_each_produce_one_review_without_conflict` | PASS |
| R01-FR5 | `agent_pipeline.tests.test_r01_recovery.R01RecoveryTests.test_r01_fr5_legacy_success_without_provenance_blocks_by_attempt` | PASS |
| R02-FR1 | `agent_pipeline.tests.test_r02_review_budget.R02ReviewBudgetTests.test_r02_fr1_fr4_identity_is_bound_to_dispatch_evidence_status_and_report` | PASS |
| R02-FR2 | `agent_pipeline.tests.test_r02_review_budget.R02ReviewBudgetTests.test_r02_fr2_fr3_failed_identity_is_not_refilled_and_new_identity_gets_one_attempt`; `agent_pipeline.tests.test_r02_review_budget.R02ReviewBudgetTests.test_r02_fr2_crash_after_dispatch_does_not_refund_identity`; `agent_pipeline.tests.test_r_cleanup_remediation.D2ReviewInvalidationAndBudgetTests.test_d2_failing_rereview_is_dispatched_once_despite_repeated_resume` | PASS |
| R02-FR3 | `agent_pipeline.tests.test_r02_review_budget.R02ReviewBudgetTests.test_r02_fr2_fr3_failed_identity_is_not_refilled_and_new_identity_gets_one_attempt`; `agent_pipeline.tests.test_r_cleanup_remediation.D2ReviewInvalidationAndBudgetTests.test_d2_failing_rereview_is_dispatched_once_despite_repeated_resume` | PASS |
| R02-FR4 | `agent_pipeline.tests.test_r02_review_budget.R02ReviewBudgetTests.test_r02_fr1_fr4_identity_is_bound_to_dispatch_evidence_status_and_report` | PASS |
| R03-FR1 | `agent_pipeline.tests.test_r03_failed_writer_approval.R03FailedWriterApprovalTests.test_r03_fr1_approval_is_durable_before_return_and_storage_failure_stops`; `agent_pipeline.tests.test_r_cleanup_remediation.D3D7FailedWriterApprovalTests.test_d3_crash_before_approval_bookkeeping_blocks_second_writer` | PASS |
| R03-FR2 | `agent_pipeline.tests.test_r03_failed_writer_approval.R03FailedWriterApprovalTests.test_r03_fr2_invalid_failed_evidence_recreates_approval_and_launches_nothing`; `agent_pipeline.tests.test_r03_failed_writer_approval.R03FailedWriterApprovalTests.test_r03_fr2_valid_success_still_recovers_automatically`; `agent_pipeline.tests.test_r_cleanup_remediation.D3D7FailedWriterApprovalTests.test_d3_crash_before_approval_bookkeeping_blocks_second_writer` | PASS |
| R03-FR3 | `agent_pipeline.tests.test_r03_failed_writer_approval.R03FailedWriterApprovalTests.test_r03_fr3_complete_source_identity_detects_required_change_classes`; `agent_pipeline.tests.test_r_cleanup_remediation.D3D7FailedWriterApprovalTests.test_d7_mode_only_change_to_dirty_file_requires_durable_approval` | PASS |
| R03-FR4 | `agent_pipeline.tests.test_r03_failed_writer_approval.R03FailedWriterApprovalTests.test_r03_fr4_unchanged_failure_retries_without_resetting_baseline_or_count` | PASS |
| R04-FR1 | `agent_pipeline.tests.test_r04_interruption.R04RealCheckInterruptionTests.test_r04_fr1_fr2_fr3_sigint_and_sigterm_stop_remaining_checks`; `agent_pipeline.tests.test_r_cleanup_remediation.D4InterruptionTests.test_d4_sigint_in_first_real_check_stops_second_check` | PASS |
| R04-FR2 | `agent_pipeline.tests.test_r04_interruption.R04RealCheckInterruptionTests.test_r04_fr1_fr2_fr3_sigint_and_sigterm_stop_remaining_checks`; `agent_pipeline.tests.test_r04_interruption.R04ControllerInterruptionTests.test_r04_fr2_fr3_interrupted_verification_blocks_overseer_and_exits_130`; `agent_pipeline.tests.test_r_cleanup_remediation.D4InterruptionTests.test_d4_interrupted_verification_invokes_no_overseer_and_exits_130` | PASS |
| R04-FR3 | `agent_pipeline.tests.test_r04_interruption.R04ControllerInterruptionTests.test_r04_fr3_interrupted_agent_attempt_is_durable_and_exits_130`; `agent_pipeline.tests.test_r04_interruption.R04ControllerInterruptionTests.test_r04_fr2_fr3_interrupted_verification_blocks_overseer_and_exits_130`; `agent_pipeline.tests.test_r_cleanup_remediation.D4InterruptionTests.test_d4_sigint_in_first_real_check_stops_second_check` | PASS |
| R04-FR4 | `agent_pipeline.tests.test_r04_interruption.R04RealCheckInterruptionTests.test_r04_fr4_timeout_is_failure_and_does_not_interrupt_invocation` | PASS |
| R05-FR1 | `agent_pipeline.tests.test_r05_manual_verification.R05ManualVerificationTests.test_r05_fr1_fr2_fr5_failed_check_blocks_binding_then_pass_allows_same_notes`; `agent_pipeline.tests.test_r_cleanup_remediation.D5ManualAcceptanceTests.test_d5_manual_accept_does_not_survive_newer_failed_check` | PASS |
| R05-FR2 | `agent_pipeline.tests.test_r05_manual_verification.R05ManualVerificationTests.test_r05_fr1_fr2_newer_failure_revokes_status_dry_run_report_and_continuation`; `agent_pipeline.tests.test_r05_manual_verification.R05ManualVerificationTests.test_r05_fr2_final_boundary_rechecks_manual_acceptance` | PASS |
| R05-FR3 | `agent_pipeline.tests.test_r05_manual_verification.R05ManualVerificationTests.test_r05_fr3_reject_and_needs_followup_do_not_require_passing_checks` | PASS |
| R05-FR4 | `agent_pipeline.tests.test_r05_manual_verification.R05ManualVerificationTests.test_r05_fr4_no_configured_checks_preserves_manual_only_acceptance` | PASS |
| R05-FR5 | `agent_pipeline.tests.test_r05_manual_verification.R05ManualVerificationTests.test_r05_fr2_fr5_invalid_manual_accept_evidence_is_named_and_actionable`; `agent_pipeline.tests.test_r_cleanup_remediation.D5ManualAcceptanceTests.test_d5_manual_accept_does_not_survive_newer_failed_check` | PASS |
| R06-FR1 | `agent_pipeline.tests.test_r06_ownership.R06AgentOwnershipTests.test_r06_fr1_fr2_missing_unreadable_and_replaced_records_block_promotion`; `agent_pipeline.tests.test_r06_ownership.R06VerificationOwnershipTests.test_r06_fr1_fr2_each_verification_check_validates_both_records`; `agent_pipeline.tests.test_r_cleanup_remediation.D6OwnershipRecordTests.test_d6_deleted_worktree_or_task_lock_fails_postcondition` | PASS |
| R06-FR2 | `agent_pipeline.tests.test_r06_ownership.R06AgentOwnershipTests.test_r06_fr1_fr2_missing_unreadable_and_replaced_records_block_promotion`; `agent_pipeline.tests.test_r06_ownership.R06AgentOwnershipTests.test_r06_fr2_verification_ownership_failure_does_not_dispatch_overseer`; `agent_pipeline.tests.test_r_cleanup_remediation.D6OwnershipRecordTests.test_d6_agent_deleting_worktree_lock_blocks_promotion_and_dispatch` | PASS |
| R06-FR3 | `agent_pipeline.tests.test_r06_ownership.R06AgentOwnershipTests.test_r06_fr3_managed_child_updates_preserve_valid_automatic_acceptance` | PASS |
| R07-FR1 | `agent_pipeline.tests.test_r07_snapshot_consistency.R07CaptureConsistencyTests.test_fr1_file_mutated_while_hashed_is_retried_and_matches_one_state`; `agent_pipeline.tests.test_r07_snapshot_consistency.R07CaptureConsistencyTests.test_fr1_file_mutated_on_every_attempt_raises_after_three_attempts`; `agent_pipeline.tests.test_r_cleanup_remediation.D8SnapshotConsistencyTests.test_d8_persistent_mid_capture_edit_raises_instead_of_mixed_identity`; `agent_pipeline.tests.test_r_cleanup_remediation.D8SnapshotConsistencyTests.test_d8_single_mid_capture_edit_retries_to_the_final_content` | PASS |
| R07-FR2 | `agent_pipeline.tests.test_r07_snapshot_consistency.R07ReasonFormattingTests.test_fr2_fr3_lists_at_most_twenty_paths_then_remaining_count`; `agent_pipeline.tests.test_r07_snapshot_consistency.R07PipelineReasonTests.test_r07_fr2_tracked_drift_names_path_in_stage6_7_8_reasons` | PASS |
| R07-FR3 | `agent_pipeline.tests.test_r07_snapshot_consistency.R07VerificationCheckOutputTests.test_fr2_fr3_unignored_check_output_names_path_and_ignore_hint`; `agent_pipeline.tests.test_r07_snapshot_consistency.R07PipelineReasonTests.test_r07_fr2_fr3_check_output_during_verification_names_path_and_hint` | PASS |
| R08-FR1 | `agent_pipeline.tests.test_c09_cleanup_gate.C09CleanupGateRecordTests.test_r08_fr1_fr3_every_defect_is_closed_by_a_passing_remediation_regression` | PASS |
| R08-FR2 | `agent_pipeline.tests.test_c09_cleanup_gate.C09CleanupGateRecordTests.test_r08_fr2_gate_requires_every_c_and_r_row_to_pass`; `agent_pipeline.tests.test_c09_cleanup_gate.C09CleanupGateRecordTests.test_c09_fr4_every_requirement_maps_to_loadable_passing_evidence` | PASS |
| R08-FR3 | `agent_pipeline.tests.test_c09_cleanup_gate.C09CleanupGateRecordTests.test_r08_fr3_record_names_revision_date_and_integration_results`; `agent_pipeline.tests.test_c09_cleanup_gate.C09CleanupGateRecordTests.test_r08_fr1_fr3_every_defect_is_closed_by_a_passing_remediation_regression` | PASS |
| R08-FR4 | `agent_pipeline.tests.test_c09_cleanup_gate.C09CleanupGateRecordTests.test_r08_fr4_operator_documentation_covers_remediated_behavior` | PASS |

The specifically required cross-slice cases are mapped above: failed-write
retry approval (C03-FR4), stale-result supersession (C06-FR5), protected
control-record tampering (C07-FR4), and injected storage failure (C08-FR1 and
C08-FR5). Recovery × invalidation (D1/D2) is covered by C06-FR3, C08-FR3,
C08-FR4, and R01-FR2–FR4.

## Reopened defects

Found 2026-09-25 by probes that combine slices; each was reproduced against the
C01–C09 candidate with fake agents and temporary Git worktrees. The temporary
review probes (`probes.py`, `probes2.py`) are converted by R08 into permanent
tests with inverted expectations in
`agent_pipeline/tests/test_r_cleanup_remediation.py`; the temporary scripts
are not gate evidence.

| Defect | Severity | Observed behavior | Cause | Requirements | Fix | Permanent regression | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | High | Editing the original request after acceptance keeps the task accepted with no new agent calls. | `attempts.recover` re-acknowledges consumed inputs from current files on every run. | C06 constraints, C06-FR3, C08-FR3 | R01 | `agent_pipeline.tests.test_r_cleanup_remediation.D1ConsumedInputRecoveryTests.test_d1_request_edit_after_acceptance_revokes_and_redispatches`; `agent_pipeline.tests.test_r_cleanup_remediation.D1ConsumedInputRecoveryTests.test_d1_recovery_does_not_rewrite_acknowledged_request_hash` | CLOSED |
| D2 | High | Invalidated Stage 7 reviews are re-promoted; a second re-review blocks with "conflicting successful durable attempts"; a failing re-review is re-dispatched on every resume despite budget 1. | Recovery ignores C06 invalidation; `ensure_current_review` resets the budget with `force=True`. | C06-FR3, C08-FR3, C08-FR4 | R01, R02 | `agent_pipeline.tests.test_r_cleanup_remediation.D2ReviewInvalidationAndBudgetTests.test_d2_recovery_does_not_resurrect_invalidated_review`; `agent_pipeline.tests.test_r_cleanup_remediation.D2ReviewInvalidationAndBudgetTests.test_d2_two_review_config_changes_each_produce_one_review_without_conflict`; `agent_pipeline.tests.test_r_cleanup_remediation.D2ReviewInvalidationAndBudgetTests.test_d2_failing_rereview_is_dispatched_once_despite_repeated_resume` | CLOSED |
| D3 | High | A crash after a failed Stage 5 writer changed source lets the resumed run launch a second writer without approval. | The approval requirement is only held in memory until the final state write. | C03-FR4, C08-FR3, C08-FR4 | R03 | `agent_pipeline.tests.test_r_cleanup_remediation.D3D7FailedWriterApprovalTests.test_d3_crash_before_approval_bookkeeping_blocks_second_writer` | CLOSED |
| D4 | Med-High | Ctrl-C during verification stops the current check, but remaining checks run and the overseer agent is still called. | `run_to_files` converts the interruption into an exit code. | C05-FR1, C05-FR2 | R04 | `agent_pipeline.tests.test_r_cleanup_remediation.D4InterruptionTests.test_d4_sigint_in_first_real_check_stops_second_check`; `agent_pipeline.tests.test_r_cleanup_remediation.D4InterruptionTests.test_d4_interrupted_verification_invokes_no_overseer_and_exits_130` | CLOSED |
| D5 | Medium | A manual `Accept` stays current after a newer configured check fails. | The manual Stage 6 route never consults verification (operator decision 1). | C06-FR2 | R05 | `agent_pipeline.tests.test_r_cleanup_remediation.D5ManualAcceptanceTests.test_d5_manual_accept_does_not_survive_newer_failed_check` | CLOSED |
| D6 | Medium | An agent can delete the task or worktree lock undetected, enabling a concurrent writer. | Ownership records are excluded from, or outside, the C07 integrity check. | C07-FR4, C04 | R06 | `agent_pipeline.tests.test_r_cleanup_remediation.D6OwnershipRecordTests.test_d6_deleted_worktree_or_task_lock_fails_postcondition`; `agent_pipeline.tests.test_r_cleanup_remediation.D6OwnershipRecordTests.test_d6_agent_deleting_worktree_lock_blocks_promotion_and_dispatch` | CLOSED |
| D7 | Low | A failed writer that only changes the mode of an already-dirty file needs no approval. | The writer baseline hashes content only. | C03-FR4 | R03 | `agent_pipeline.tests.test_r_cleanup_remediation.D3D7FailedWriterApprovalTests.test_d7_mode_only_change_to_dirty_file_requires_durable_approval` | CLOSED |
| D8 | Low | A file edited mid-capture can yield an identity matching neither version. | Only HEAD and the index are re-checked after hashing. | C06-FR5 | R07 | `agent_pipeline.tests.test_r_cleanup_remediation.D8SnapshotConsistencyTests.test_d8_persistent_mid_capture_edit_raises_instead_of_mixed_identity`; `agent_pipeline.tests.test_r_cleanup_remediation.D8SnapshotConsistencyTests.test_d8_single_mid_capture_edit_retries_to_the_final_content` | CLOSED |

Operator decisions recorded the same day: configured driven-project checks bind
both Stage 6 routes; a Stage 7 re-review gets the attempt budget once per
review-input identity; source identity stays strict and stale reasons name
changed paths. The [cleanup SRS](v2-cleanup.md) wording is amended accordingly.

## Unresolved cleanup issues

None blocking this gate. D1–D8 are closed by R01–R08 and the regressions
above. V3 remains out of scope for this gate.

Deferred follow-up, recorded 2026-09-25 and outside this gate by operator
decision: when a Stage 7 review-input identity exhausts its allowance, the
blocking reason says to use `approve-retry`, but no pending approval is
created, so `approve-retry` reports "no pending approval". The budget itself
behaves as R02 requires (no refill; a new identity gets a new allowance);
only the suggested route is not actionable. `docs/USAGE.md` describes the
actual behavior.
