from __future__ import print_function

import tempfile
import unittest
from pathlib import Path

from agent_pipeline import controller
from agent_pipeline.failures import EXIT_BLOCKED, EXIT_INTERRUPTED, EXIT_SUCCESS
from agent_pipeline.policies import choose_agent
from agent_pipeline.state import new_state, orchestrator_dir


class PolicyAndFailureTests(unittest.TestCase):
    def run_scenario(self, scenario):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        task_dir = Path(tmp.name) / "policy-test"
        state = new_state("policy-test", "run-test")
        code = controller.run_scenario(task_dir, "policy-test", state, scenario)
        return code, state, task_dir

    def assert_blocks_stage_02(self, scenario, failure_class):
        code, state, task_dir = self.run_scenario(scenario)
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(state["state"], "blocked")
        self.assertEqual(state["last_failure"]["stage"], "02")
        self.assertEqual(state["last_failure"]["failure_class"], failure_class)
        self.assertTrue(list((orchestrator_dir(task_dir) / "failed").glob("02-*")))

    def test_malformed_output_retry_budget_blocks(self):
        self.assert_blocks_stage_02({"actions": {"02": "malformed_artifact"}, "attempt_budget": 1}, "malformed_artifact")

    def test_empty_output_retry_budget_blocks(self):
        self.assert_blocks_stage_02({"actions": {"02": "empty_output"}, "attempt_budget": 1}, "empty_output")

    def test_timeout_retry_budget_blocks(self):
        self.assert_blocks_stage_02({"actions": {"02": "timeout"}, "attempt_budget": 1}, "timeout")

    def test_max_turn_useful_partial_gets_completion_retry(self):
        code, state, task_dir = self.run_scenario({"actions": {"02": "max_turns_useful_partial"}})

        self.assertEqual(code, EXIT_SUCCESS)
        self.assertEqual(state["state"], "complete")
        self.assertEqual(state["attempts"]["02_completion_retry"], 1)
        self.assertTrue(list((orchestrator_dir(task_dir) / "failed").glob("02-*")))

    def test_max_turn_unusable_requires_approval(self):
        code, state, task_dir = self.run_scenario({"actions": {"02": "max_turns_unusable"}})

        self.assertEqual(code, EXIT_BLOCKED)
        self.assertEqual(state["state"], "awaiting_retry_approval")
        self.assertEqual(state["pending_approval"]["stage"], "02")
        self.assertTrue(list((orchestrator_dir(task_dir) / "failed").glob("02-*")))

    def test_rate_limit_without_reset_blocks(self):
        self.assert_blocks_stage_02({"actions": {"02": "rate_limit_no_reset"}}, "rate_limit")

    def test_process_interruption_preserves_readiness(self):
        code, state, task_dir = self.run_scenario({"actions": {"02": "process_interrupted"}})

        self.assertEqual(code, EXIT_INTERRUPTED)
        self.assertEqual(state["state"], "ready")
        self.assertEqual(state["last_failure"]["failure_class"], "process_interrupted")
        self.assertTrue(list((orchestrator_dir(task_dir) / "failed").glob("02-*")))

    def test_usage_limit_falls_back_to_next_agent(self):
        code, state, _task_dir = self.run_scenario({"actions": {"02": ["usage_limit", "success"]}})

        self.assertEqual(code, EXIT_SUCCESS)
        self.assertEqual(state["state"], "complete")
        self.assertEqual(state["fallback_events"][0]["stage"], "02")
        self.assertEqual(state["fallback_events"][0]["agent"], "agy")

    def test_rate_limit_with_reset_falls_back_to_next_agent(self):
        code, state, _task_dir = self.run_scenario({"actions": {"02": ["rate_limit_with_reset", "success"]}})

        self.assertEqual(code, EXIT_SUCCESS)
        self.assertEqual(state["state"], "complete")
        self.assertEqual(state["fallback_events"][0]["agent"], "agy")

    def test_strict_reviewer_independence_can_block_routing(self):
        state = new_state("policy-test")
        state["run_unavailable_agents"] = {"agy": {"reason": "usage_limit"}}

        route = choose_agent("04_gate", state, {"safety_mode": "strict"}, {"04": "claude"})

        self.assertIsNone(route)

    def test_continuity_mode_records_degraded_same_agent_review(self):
        code, state, _task_dir = self.run_scenario(
            {
                "actions": {
                    "04": ["usage_limit", "success"],
                    "04_gate": ["usage_limit", "success"],
                },
                "safety_mode": "continuity",
                "allow_degraded_same_agent_review": True,
            }
        )

        self.assertEqual(code, EXIT_SUCCESS)
        self.assertTrue(
            any(event.get("reason") == "degraded_same_agent_review" for event in state["fallback_events"])
        )


if __name__ == "__main__":
    unittest.main()
