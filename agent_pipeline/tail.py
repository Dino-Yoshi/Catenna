"""Read-only live/post-hoc visibility into a task's agent-CLI runs.

Reads `.orchestrator/runs/<base>.stdout` (+ `.json` sidecar once the run
finishes) written by real_runner.invoke_agent. Never mutates task state and
never acquires the task lock, so `pipeline-tail`/`pipeline-brief` are safe
to run concurrently with an in-progress `pipeline-run`.
"""

from __future__ import print_function

import json
import time
from pathlib import Path

from . import stream_events
from .state import orchestrator_dir


def runs_dir(task_dir):
    return orchestrator_dir(task_dir) / "runs"


def list_run_files(task_dir):
    """All *.stdout run files for a task, newest mtime first."""
    directory = runs_dir(task_dir)
    if not directory.exists():
        return []
    files = [p for p in directory.glob("*.stdout") if p.is_file()]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def _matches(path, stage, run_id):
    name = path.name
    if stage and not name.startswith(stage + "-pass-"):
        return False
    if run_id and not name.endswith("-" + run_id + ".stdout"):
        return False
    return True


def locate(task_dir, stage=None, run_id=None):
    """Pick the target .stdout file.

    With an explicit stage/run_id filter: newest match. Otherwise: the
    newest file still missing its .json sidecar (in progress), else the
    newest file overall (most recent completed run). None if no runs exist.
    """
    candidates = list_run_files(task_dir)
    if stage or run_id:
        candidates = [p for p in candidates if _matches(p, stage, run_id)]
        return candidates[0] if candidates else None
    in_progress = [p for p in candidates if not p.with_suffix(".json").exists()]
    if in_progress:
        return in_progress[0]
    return candidates[0] if candidates else None


def _sidecar(stdout_path):
    metadata_path = stdout_path.with_suffix(".json")
    if not metadata_path.exists():
        return None
    try:
        return json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def follow(task_dir, stage=None, run_id=None, poll_interval=0.4, print_fn=print, max_wait_seconds=None):
    """Tail a run's stdout file, printing one summarized line per event as
    it arrives. Stops when the .json sidecar appears (run finished) or on
    KeyboardInterrupt. Returns a short status string."""
    stdout_path = locate(task_dir, stage, run_id)
    if stdout_path is None:
        print_fn("no runs found for this task yet")
        return "no_runs"

    print_fn("tailing %s" % stdout_path.name)
    agent = None
    buffer = ""
    waited = 0.0
    with open(str(stdout_path), "r", encoding="utf-8", errors="replace") as handle:
        try:
            while True:
                chunk = handle.read()
                if chunk:
                    buffer += chunk
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except ValueError:
                            continue
                        if agent is None:
                            agent = stream_events.detect_agent(obj)
                        summary = stream_events.summarize_event(agent, obj)
                        if summary:
                            print_fn(summary)
                    waited = 0.0
                    continue
                if _sidecar(stdout_path) is not None:
                    print_fn("run complete")
                    return "complete"
                time.sleep(poll_interval)
                waited += poll_interval
                if max_wait_seconds is not None and waited >= max_wait_seconds:
                    print_fn("stopped watching (max wait reached); run may still be in progress")
                    return "timed_out"
        except KeyboardInterrupt:
            print_fn("stopped watching; run may still be in progress")
            return "interrupted"


def _safe_read(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def brief(task_dir, stage=None, run_id=None, print_fn=print):
    """Print a compact summary of a run (in-progress or finished)."""
    stdout_path = locate(task_dir, stage, run_id)
    if stdout_path is None:
        print_fn("no runs found for this task yet")
        return "no_runs"

    stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace")
    agent = stream_events.detect_agent_from_stream(stdout_text)
    metadata = _sidecar(stdout_path)

    print_fn("run: %s" % stdout_path.name)
    print_fn("agent: %s" % (agent or "unknown"))
    if metadata:
        print_fn("stage: %s" % metadata.get("stage"))
        duration_seconds = metadata.get("duration_seconds")
        if duration_seconds is None:
            duration_seconds = 0.0
        print_fn("duration: %.1fs" % duration_seconds)
        print_fn("exit_code: %s" % metadata.get("exit_code"))
        print_fn("failure_class: %s" % (metadata.get("failure_class") or "none"))
        if metadata.get("usage"):
            print_fn("usage: " + json.dumps(metadata["usage"], sort_keys=True))
        if metadata.get("reasoning_path"):
            print_fn("reasoning_path: " + metadata["reasoning_path"])
            reasoning_text = _safe_read(metadata["reasoning_path"])
            if reasoning_text:
                excerpt = reasoning_text[:300] + ("..." if len(reasoning_text) > 300 else "")
                print_fn("reasoning: " + excerpt.replace("\n", " ").strip())
    else:
        print_fn("status: in progress (no metadata sidecar yet)")

    counts = {}
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        key = obj.get("type") or obj.get("event") or "unknown"
        counts[key] = counts.get(key, 0) + 1
    if counts:
        print_fn("events: " + ", ".join("%s=%d" % (k, v) for k, v in sorted(counts.items())))

    text = stream_events.final_text(agent, stdout_text)
    if text:
        excerpt = text[:300] + ("..." if len(text) > 300 else "")
        print_fn("final text: " + excerpt.replace("\n", " "))
    return "ok"
