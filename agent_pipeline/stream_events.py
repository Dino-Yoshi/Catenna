"""Parsing/summarizing helpers for the JSONL event streams emitted by
codex/claude/agy when invoked with their respective streaming flags
(``codex exec --json``, ``claude --output-format stream-json``,
``agy --output-format stream-json``).

Each CLI uses a different event schema. This module is the single place
that knows about all three, so real_runner.py (candidate extraction,
failure classification) and tail.py (live tail, brief summaries) share one
interpretation of the same bytes instead of drifting apart.

Every function here is best-effort: unparseable lines are skipped and
unrecognized event shapes degrade to ``None``/a raw echo rather than
raising, since CLI JSON schemas can change across versions.
"""

from __future__ import print_function

import json

from .failures import (
    FAILURE_CLASS_MAX_TURNS,
    FAILURE_CLASS_PROCESS_INTERRUPTED,
    FAILURE_CLASS_TIMEOUT,
    FAILURE_CLASS_UNKNOWN_FAILURE,
)


def parse_json_lines(stdout_text):
    events = []
    for line in (stdout_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def _events(stdout_text, events=None):
    if events is not None:
        return events
    return parse_json_lines(stdout_text)


def detect_agent(obj):
    """Sniff which CLI produced a single parsed JSON event line."""
    if not isinstance(obj, dict):
        return None
    if "event" in obj:
        return "agy"
    if obj.get("type") == "thread.started":
        return "codex"
    if "type" in obj:
        return "claude"
    return None


def detect_agent_from_stream(stdout_text, events=None):
    for obj in _events(stdout_text, events):
        agent = detect_agent(obj)
        if agent:
            return agent
    return None


def final_text(agent, stdout_text, events=None):
    """Extract the final response text from a completed JSONL stream.

    Returns None if the agent is unrecognized or no final-text event is
    found (including: stdout isn't JSONL at all, e.g. a plain-text
    fixture) so callers can fall back to their own default behavior.
    """
    events = _events(stdout_text, events)
    if agent is None:
        agent = detect_agent_from_stream(stdout_text, events=events)
    if agent is None:
        return None
    text = None
    for obj in events:
        if agent == "codex":
            if obj.get("type") == "item.completed":
                item = obj.get("item") or {}
                if item.get("type") == "agent_message" and item.get("text"):
                    text = item["text"]
        elif agent == "claude":
            if obj.get("type") == "result" and obj.get("result"):
                text = obj["result"]
        elif agent == "agy":
            if obj.get("event") == "result":
                result = obj.get("result") or {}
                if result.get("response"):
                    text = result["response"]
    return text


_USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def _normalize_usage(raw_usage, total_cost_usd=None, cache_read_field="cache_read_input_tokens", cache_creation_field="cache_creation_input_tokens"):
    if not isinstance(raw_usage, dict) and total_cost_usd is None:
        return None
    raw_usage = raw_usage if isinstance(raw_usage, dict) else {}
    normalized = {
        "input_tokens": raw_usage.get("input_tokens"),
        "output_tokens": raw_usage.get("output_tokens"),
        "cache_read_tokens": raw_usage.get(cache_read_field),
        "cache_creation_tokens": raw_usage.get(cache_creation_field),
        "total_cost_usd": total_cost_usd if total_cost_usd is not None else raw_usage.get("total_cost_usd"),
    }
    if all(value is None for value in normalized.values()):
        return None
    return normalized


def usage_summary(agent, stdout_text, events=None):
    """Best-effort token/cost usage extracted from a completed JSONL stream.

    Returns None if the agent is unrecognized or no usage-bearing event is
    found, matching final_text/structured_failure's contract: never raises,
    never guesses. The last usage-bearing event in the stream wins, same as
    final_text's "last one wins" handling of streamed text.
    """
    events = _events(stdout_text, events)
    if agent is None:
        agent = detect_agent_from_stream(stdout_text, events=events)
    if agent is None:
        return None
    usage = None
    for obj in events:
        if agent == "codex":
            if obj.get("type") == "turn.completed" and isinstance(obj.get("usage"), dict):
                usage = _normalize_usage(obj["usage"], cache_read_field="cached_input_tokens", cache_creation_field="cache_write_input_tokens")
        elif agent == "claude":
            if obj.get("type") == "result":
                usage = _normalize_usage(obj.get("usage"), total_cost_usd=obj.get("total_cost_usd"))
        elif agent == "agy":
            if obj.get("event") == "result":
                result = obj.get("result") or {}
                usage = _normalize_usage(result.get("usage"), total_cost_usd=result.get("total_cost_usd") or result.get("cost_usd"))
    return usage


def reasoning_summary(agent, stdout_text, events=None):
    """Best-effort chain-of-thought/reasoning text extracted from a
    completed JSONL stream -- the "peer into thinking" signal, kept
    separate from final_text since a reader wants to distinguish the
    answer from what led to it.

    Returns None if the agent is unrecognized or no reasoning-bearing
    event is found, matching usage_summary/final_text's contract: never
    raises, never guesses. Multiple reasoning segments (e.g. codex's
    distinct reasoning items, or claude's distinct thinking blocks) are
    joined with a blank line, in stream order.

    agy has no known reasoning-bearing event in its stream-json schema
    today (its step_update events carry no reasoning text) -- always
    returns None for agy, same as an unrecognized agent.
    """
    events = _events(stdout_text, events)
    if agent is None:
        agent = detect_agent_from_stream(stdout_text, events=events)
    if agent is None:
        return None
    if agent == "codex":
        return _reasoning_codex(stdout_text, events=events)
    if agent == "claude":
        return _reasoning_claude(stdout_text, events=events)
    return None


def _reasoning_codex(stdout_text, events=None):
    segments = []
    for obj in _events(stdout_text, events):
        if obj.get("type") != "item.completed":
            continue
        item = obj.get("item") or {}
        if item.get("type") == "reasoning" and item.get("text"):
            segments.append(item["text"])
    return "\n\n".join(segments) if segments else None


def _reasoning_claude(stdout_text, events=None):
    blocks = {}
    order = []
    for obj in _events(stdout_text, events):
        if obj.get("type") != "stream_event":
            continue
        event = obj.get("event") or {}
        etype = event.get("type")
        if etype == "content_block_start":
            if (event.get("content_block") or {}).get("type") == "thinking":
                index = event.get("index")
                if index not in blocks:
                    blocks[index] = []
                    order.append(index)
        elif etype == "content_block_delta":
            index = event.get("index")
            if index not in blocks:
                continue
            delta = event.get("delta") or {}
            if delta.get("type") == "thinking_delta" and delta.get("thinking"):
                blocks[index].append(delta["thinking"])
    segments = ["".join(blocks[index]) for index in order if blocks[index]]
    return "\n\n".join(segments) if segments else None


_CLAUDE_ERROR_SUBTYPE_MAP = {
    "error_max_turns": FAILURE_CLASS_MAX_TURNS,
    "error_during_execution": FAILURE_CLASS_UNKNOWN_FAILURE,
}

_AGY_STATUS_MAP = {
    "MAX_TURNS": FAILURE_CLASS_MAX_TURNS,
    "TIMEOUT": FAILURE_CLASS_TIMEOUT,
    "ERROR": FAILURE_CLASS_UNKNOWN_FAILURE,
    "CANCELLED": FAILURE_CLASS_PROCESS_INTERRUPTED,
}


def structured_failure(agent, stdout_text, events=None):
    """Best-effort structured failure classification from a JSONL stream.

    Returns None (never a made-up guess) when nothing recognizable is
    found, so callers fall back to their existing substring-based
    classification untouched.
    """
    events = _events(stdout_text, events)
    if agent is None:
        agent = detect_agent_from_stream(stdout_text, events=events)
    if agent is None:
        return None
    classification = None
    for obj in events:
        if agent == "claude":
            if obj.get("type") == "result":
                subtype = obj.get("subtype")
                if obj.get("is_error") and subtype:
                    classification = _CLAUDE_ERROR_SUBTYPE_MAP.get(subtype, FAILURE_CLASS_UNKNOWN_FAILURE)
        elif agent == "agy":
            if obj.get("event") == "result":
                status = (obj.get("result") or {}).get("status")
                if status and status != "SUCCESS":
                    classification = _AGY_STATUS_MAP.get(status, FAILURE_CLASS_UNKNOWN_FAILURE)
        elif agent == "codex":
            if obj.get("type") == "turn.failed":
                reason = str((obj.get("error") or {}).get("message") or "").lower()
                if "max" in reason and "turn" in reason:
                    classification = FAILURE_CLASS_MAX_TURNS
                else:
                    classification = FAILURE_CLASS_UNKNOWN_FAILURE
    return classification


def summarize_event(agent, obj, verbose=False):
    """One short human-readable line for a single parsed event, or None
    if the event isn't worth printing (e.g. a raw text delta)."""
    if not isinstance(obj, dict):
        return None
    if agent is None:
        agent = detect_agent(obj)
    if agent == "codex":
        return _summarize_codex(obj, verbose=verbose)
    if agent == "claude":
        return _summarize_claude(obj, verbose=verbose)
    if agent == "agy":
        return _summarize_agy(obj, verbose=verbose)
    return "[unknown] " + _short(obj, verbose=verbose)


def _summarize_codex(obj, verbose=False):
    kind = obj.get("type")
    if kind == "thread.started":
        return "thread started"
    if kind == "turn.started":
        return "turn started"
    if kind == "turn.completed":
        return "turn completed"
    if kind == "turn.failed":
        reason = (obj.get("error") or {}).get("message")
        return "turn failed: %s" % (reason or "unknown reason")
    if kind == "item.completed":
        item = obj.get("item") or {}
        item_type = item.get("type")
        if item_type == "agent_message":
            return "message: " + _truncate(item.get("text"), verbose=verbose)
        if item_type in ("command_execution", "function_call"):
            return "tool call: " + _truncate(item.get("command") or item.get("name") or item_type, verbose=verbose)
        return "item completed: " + str(item_type)
    return None


def _summarize_claude(obj, verbose=False):
    kind = obj.get("type")
    if kind == "system" and obj.get("subtype") == "init":
        return "session started"
    if kind == "result":
        if obj.get("is_error"):
            return "result: error (%s)" % obj.get("subtype")
        return "result: " + _truncate(obj.get("result"), verbose=verbose)
    if kind == "stream_event":
        event = obj.get("event") or {}
        etype = event.get("type")
        if etype == "content_block_start":
            block_type = (event.get("content_block") or {}).get("type")
            if block_type == "thinking":
                return "thinking..."
            if block_type == "text":
                return "responding..."
            if block_type == "tool_use":
                return "tool call: " + str((event.get("content_block") or {}).get("name"))
        return None
    return None


def _summarize_agy(obj, verbose=False):
    kind = obj.get("event")
    if kind == "init":
        return "session started"
    if kind == "step_update":
        step = obj.get("step_update") or {}
        state = step.get("state")
        step_type = step.get("step_type")
        if state == "ACTIVE":
            return "step started: " + str(step_type)
        if state == "DONE":
            return "step done: " + str(step_type)
        return "step %s: %s" % (state, step_type)
    if kind == "result":
        result = obj.get("result") or {}
        if result.get("status") and result["status"] != "SUCCESS":
            return "result: %s" % result["status"]
        return "result: " + _truncate(result.get("response"), verbose=verbose)
    return None


def _truncate(text, limit=120, verbose=False):
    text = str(text or "").replace("\n", " ").strip()
    if verbose:
        return text
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _short(obj, verbose=False):
    try:
        text = json.dumps(obj)
    except Exception:
        text = str(obj)
    return text if verbose else text[:120]
