from __future__ import print_function

import unittest

from agent_pipeline import stream_events


CODEX_STREAM = "\n".join(
    [
        '{"type":"thread.started","thread_id":"t1"}',
        '{"type":"turn.started"}',
        '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"# Stage 2 - Technical specification\\nfake body"}}',
        '{"type":"turn.completed","usage":{"input_tokens":10}}',
    ]
)

CODEX_MAX_TURNS_STREAM = "\n".join(
    [
        '{"type":"thread.started","thread_id":"t1"}',
        '{"type":"turn.started"}',
        '{"type":"turn.failed","error":{"message":"Reached maximum turns for this run"}}',
    ]
)

CLAUDE_STREAM = "\n".join(
    [
        '{"type":"system","subtype":"init","cwd":"/repo"}',
        '{"type":"stream_event","event":{"type":"content_block_start","index":0,"content_block":{"type":"thinking"}}}',
        '{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"partial"}}}',
        '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"result":"final claude text"}',
    ]
)

CLAUDE_ERROR_STREAM = "\n".join(
    [
        '{"type":"system","subtype":"init","cwd":"/repo"}',
        '{"type":"result","subtype":"error_max_turns","is_error":true,"num_turns":20}',
    ]
)

CLAUDE_STREAM_WITH_USAGE = "\n".join(
    [
        '{"type":"system","subtype":"init","cwd":"/repo"}',
        '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"result":"final claude text",'
        '"total_cost_usd":0.0421,"usage":{"input_tokens":120,"output_tokens":45,'
        '"cache_creation_input_tokens":10,"cache_read_input_tokens":5}}',
    ]
)

AGY_STREAM_WITH_USAGE = "\n".join(
    [
        '{"event":"init","conversation_id":"c1","init":{"cwd":"/repo"}}',
        '{"event":"result","result":{"conversation_id":"c1","status":"SUCCESS","response":"final agy text",'
        '"usage":{"input_tokens":30,"output_tokens":12}}}',
    ]
)

AGY_STREAM = "\n".join(
    [
        '{"event":"init","conversation_id":"c1","init":{"cwd":"/repo"}}',
        '{"event":"step_update","step_update":{"conversation_id":"c1","step_index":0,"state":"ACTIVE","step_type":"agent_turn"}}',
        '{"event":"step_update","step_update":{"conversation_id":"c1","step_index":0,"state":"DONE","step_type":"agent_turn"}}',
        '{"event":"result","result":{"conversation_id":"c1","status":"SUCCESS","response":"final agy text"}}',
    ]
)

AGY_ERROR_STREAM = "\n".join(
    [
        '{"event":"init","conversation_id":"c1","init":{"cwd":"/repo"}}',
        '{"event":"result","result":{"conversation_id":"c1","status":"MAX_TURNS","response":""}}',
    ]
)

CODEX_STREAM_WITH_REASONING = "\n".join(
    [
        '{"type":"thread.started","thread_id":"t1"}',
        '{"type":"turn.started"}',
        '{"type":"item.completed","item":{"id":"item_r","type":"reasoning","text":"first I checked the schema"}}',
        '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"final answer"}}',
        '{"type":"turn.completed","usage":{"input_tokens":10}}',
    ]
)

CODEX_STREAM_WITH_CACHE_USAGE = "\n".join(
    [
        '{"type":"thread.started","thread_id":"t1"}',
        '{"type":"turn.started"}',
        '{"type":"item.completed","item":{"id":"item_r","type":"reasoning","text":"not usage"}}',
        '{"type":"turn.completed","usage":{"input_tokens":25,"output_tokens":5,'
        '"cached_input_tokens":75,"cache_write_input_tokens":10,"reasoning_output_tokens":99}}',
    ]
)

CLAUDE_STREAM_WITH_THINKING = "\n".join(
    [
        '{"type":"system","subtype":"init","cwd":"/repo"}',
        '{"type":"stream_event","event":{"type":"content_block_start","index":0,"content_block":{"type":"thinking"}}}',
        '{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"step one, "}}}',
        '{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"step two"}}}',
        '{"type":"stream_event","event":{"type":"content_block_stop","index":0}}',
        '{"type":"stream_event","event":{"type":"content_block_start","index":1,"content_block":{"type":"text"}}}',
        '{"type":"stream_event","event":{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"final"}}}',
        '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"result":"final claude text"}',
    ]
)

PLAIN_TEXT_STREAM = "# Stage 2 - Technical specification\nplain markdown, not json\n"


class DetectAgentTests(unittest.TestCase):
    def test_detects_codex(self):
        self.assertEqual(stream_events.detect_agent_from_stream(CODEX_STREAM), "codex")

    def test_detects_claude(self):
        self.assertEqual(stream_events.detect_agent_from_stream(CLAUDE_STREAM), "claude")

    def test_detects_agy(self):
        self.assertEqual(stream_events.detect_agent_from_stream(AGY_STREAM), "agy")

    def test_plain_text_has_no_agent(self):
        self.assertIsNone(stream_events.detect_agent_from_stream(PLAIN_TEXT_STREAM))

    def test_helpers_reuse_supplied_events_when_agent_is_omitted(self):
        mixed = "not json\n" + CLAUDE_STREAM_WITH_USAGE + "\n{bad json"
        events = stream_events.parse_json_lines(mixed)
        self.assertEqual(stream_events.detect_agent_from_stream("ignored", events=events), "claude")
        self.assertEqual(stream_events.final_text(None, "ignored", events=events), "final claude text")
        self.assertEqual(stream_events.usage_summary(None, "ignored", events=events)["input_tokens"], 120)
        self.assertIsNone(stream_events.structured_failure(None, "ignored", events=events))


class FinalTextTests(unittest.TestCase):
    def test_codex_final_text(self):
        text = stream_events.final_text("codex", CODEX_STREAM)
        self.assertIn("Stage 2 - Technical specification", text)

    def test_claude_final_text(self):
        self.assertEqual(stream_events.final_text("claude", CLAUDE_STREAM), "final claude text")

    def test_agy_final_text(self):
        self.assertEqual(stream_events.final_text("agy", AGY_STREAM), "final agy text")

    def test_plain_text_returns_none(self):
        self.assertIsNone(stream_events.final_text("claude", PLAIN_TEXT_STREAM))
        self.assertIsNone(stream_events.final_text(None, PLAIN_TEXT_STREAM))

    def test_auto_detects_agent_when_not_given(self):
        self.assertEqual(stream_events.final_text(None, CLAUDE_STREAM), "final claude text")


class StructuredFailureTests(unittest.TestCase):
    def test_claude_max_turns_error(self):
        self.assertEqual(stream_events.structured_failure("claude", CLAUDE_ERROR_STREAM), "max_turns")

    def test_agy_max_turns_status(self):
        self.assertEqual(stream_events.structured_failure("agy", AGY_ERROR_STREAM), "max_turns")

    def test_codex_max_turns_failure(self):
        self.assertEqual(stream_events.structured_failure("codex", CODEX_MAX_TURNS_STREAM), "max_turns")

    def test_success_stream_has_no_failure(self):
        self.assertIsNone(stream_events.structured_failure("claude", CLAUDE_STREAM))
        self.assertIsNone(stream_events.structured_failure("agy", AGY_STREAM))
        self.assertIsNone(stream_events.structured_failure("codex", CODEX_STREAM))

    def test_plain_text_has_no_structured_failure(self):
        self.assertIsNone(stream_events.structured_failure(None, PLAIN_TEXT_STREAM))


class SummarizeEventTests(unittest.TestCase):
    def test_codex_message_event_is_summarized(self):
        obj = {"type": "item.completed", "item": {"type": "agent_message", "text": "hello"}}
        summary = stream_events.summarize_event("codex", obj)
        self.assertIn("message", summary)

    def test_claude_text_block_start_is_summarized(self):
        obj = {"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "text"}}}
        self.assertEqual(stream_events.summarize_event("claude", obj), "responding...")

    def test_claude_result_event_is_summarized(self):
        obj = {"type": "result", "is_error": False, "result": "final text"}
        summary = stream_events.summarize_event("claude", obj)
        self.assertIn("final text", summary)

    def test_agy_step_update_is_summarized(self):
        obj = {"event": "step_update", "step_update": {"state": "ACTIVE", "step_type": "agent_turn"}}
        summary = stream_events.summarize_event("agy", obj)
        self.assertIn("agent_turn", summary)

    def test_unrecognized_event_does_not_raise(self):
        self.assertIsNone(stream_events.summarize_event("codex", {"type": "something.new.and.unknown"}))
        self.assertIsNone(stream_events.summarize_event("claude", {"type": "something.new.and.unknown"}))
        self.assertIsNone(stream_events.summarize_event("agy", {"event": "something.new.and.unknown"}))

    def test_non_dict_does_not_raise(self):
        self.assertIsNone(stream_events.summarize_event("codex", "not a dict"))
        self.assertIsNone(stream_events.summarize_event("codex", None))

    def test_non_verbose_truncates_message_summary(self):
        obj = {"type": "item.completed", "item": {"type": "agent_message", "text": "x" * 130}}
        summary = stream_events.summarize_event("codex", obj)
        self.assertTrue(summary.endswith("..."))
        self.assertLess(len(summary), 150)

    def test_verbose_keeps_full_normalized_message_summary(self):
        text = ("x" * 130) + "\nsecond line"
        obj = {"type": "item.completed", "item": {"type": "agent_message", "text": text}}
        summary = stream_events.summarize_event("codex", obj, verbose=True)
        self.assertIn(("x" * 130) + " second line", summary)
        self.assertNotIn("\n", summary)
        self.assertFalse(summary.endswith("..."))

    def test_unknown_event_short_verbose_keeps_full_json(self):
        obj = {"payload": "x" * 150}
        summary = stream_events.summarize_event(None, obj, verbose=True)
        self.assertIn("x" * 150, summary)


class UsageSummaryTests(unittest.TestCase):
    def test_codex_usage_extracted(self):
        usage = stream_events.usage_summary("codex", CODEX_STREAM)
        self.assertEqual(usage["input_tokens"], 10)

    def test_codex_cache_usage_uses_codex_field_names(self):
        usage = stream_events.usage_summary("codex", CODEX_STREAM_WITH_CACHE_USAGE)
        self.assertEqual(usage["input_tokens"], 25)
        self.assertEqual(usage["output_tokens"], 5)
        self.assertEqual(usage["cache_read_tokens"], 75)
        self.assertEqual(usage["cache_creation_tokens"], 10)
        self.assertNotIn("reasoning_output_tokens", usage)

    def test_claude_usage_and_cost_extracted(self):
        usage = stream_events.usage_summary("claude", CLAUDE_STREAM_WITH_USAGE)
        self.assertEqual(usage["input_tokens"], 120)
        self.assertEqual(usage["output_tokens"], 45)
        self.assertEqual(usage["cache_creation_tokens"], 10)
        self.assertEqual(usage["cache_read_tokens"], 5)
        self.assertAlmostEqual(usage["total_cost_usd"], 0.0421)

    def test_agy_usage_extracted(self):
        usage = stream_events.usage_summary("agy", AGY_STREAM_WITH_USAGE)
        self.assertEqual(usage["input_tokens"], 30)
        self.assertEqual(usage["output_tokens"], 12)

    def test_claude_result_without_usage_returns_none(self):
        self.assertIsNone(stream_events.usage_summary("claude", CLAUDE_STREAM))

    def test_agy_result_without_usage_returns_none(self):
        self.assertIsNone(stream_events.usage_summary("agy", AGY_STREAM))

    def test_unrecognized_agent_returns_none(self):
        self.assertIsNone(stream_events.usage_summary(None, PLAIN_TEXT_STREAM))

    def test_auto_detects_agent_when_not_given(self):
        usage = stream_events.usage_summary(None, CLAUDE_STREAM_WITH_USAGE)
        self.assertEqual(usage["input_tokens"], 120)


class ReasoningSummaryTests(unittest.TestCase):
    def test_codex_reasoning_extracted(self):
        text = stream_events.reasoning_summary("codex", CODEX_STREAM_WITH_REASONING)
        self.assertEqual(text, "first I checked the schema")

    def test_claude_thinking_deltas_accumulated_in_order(self):
        text = stream_events.reasoning_summary("claude", CLAUDE_STREAM_WITH_THINKING)
        self.assertEqual(text, "step one, step two")

    def test_agy_has_no_known_reasoning_event(self):
        self.assertIsNone(stream_events.reasoning_summary("agy", AGY_STREAM_WITH_USAGE))

    def test_codex_stream_without_reasoning_returns_none(self):
        self.assertIsNone(stream_events.reasoning_summary("codex", CODEX_STREAM))

    def test_claude_stream_without_thinking_returns_none(self):
        self.assertIsNone(stream_events.reasoning_summary("claude", CLAUDE_STREAM_WITH_USAGE))

    def test_plain_text_returns_none(self):
        self.assertIsNone(stream_events.reasoning_summary(None, PLAIN_TEXT_STREAM))

    def test_auto_detects_agent_when_not_given(self):
        text = stream_events.reasoning_summary(None, CODEX_STREAM_WITH_REASONING)
        self.assertEqual(text, "first I checked the schema")


if __name__ == "__main__":
    unittest.main()
