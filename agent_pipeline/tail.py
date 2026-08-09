"""Read-only live/post-hoc visibility into a task's agent and verification runs.

Reads `.orchestrator/runs/<base>.stdout` (+ `.json` sidecar once the run
finishes) written by real_runner.invoke_agent, and unfiltered tails can also
follow `.orchestrator/verification_runs/<base>.stdout` files. Never mutates
task state and never acquires the task lock, so `pipeline-tail`/`pipeline-brief`
are safe to run concurrently with an in-progress `pipeline-run`.
"""

from __future__ import print_function

import json
import time
from pathlib import Path

from . import stream_events
from . import state
from .state import CorruptState, orchestrator_dir


def runs_dir(task_dir):
    return orchestrator_dir(task_dir) / "runs"


def list_run_files(task_dir):
    """All *.stdout run files for a task, newest mtime first."""
    directory = runs_dir(task_dir)
    if not directory.exists():
        return []
    files = [p for p in directory.glob("*.stdout") if p.is_file()]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def verification_runs_path(task_dir):
    return orchestrator_dir(task_dir) / "verification_runs"


def list_verification_run_files(task_dir):
    """All *.stdout verification files for a task, newest mtime first.

    Read-only: does not create `.orchestrator` or `verification_runs`.
    """
    directory = verification_runs_path(task_dir)
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

    With an explicit stage/run_id filter: newest matching pipeline run.
    Otherwise: the newest pipeline or verification file still missing its
    .json sidecar (in progress), else the newest file overall. None if no
    stdout files exist.
    """
    if stage or run_id:
        candidates = list_run_files(task_dir)
        candidates = [p for p in candidates if _matches(p, stage, run_id)]
        return candidates[0] if candidates else None
    candidates = sorted(list_run_files(task_dir) + list_verification_run_files(task_dir), key=lambda p: p.stat().st_mtime, reverse=True)
    in_progress = [p for p in candidates if not p.with_suffix(".json").exists()]
    if in_progress:
        return in_progress[0]
    return candidates[0] if candidates else None


def _locate_pipeline_run(task_dir, stage=None, run_id=None):
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


def _timed_out(waited, max_wait_seconds):
    return max_wait_seconds is not None and waited >= max_wait_seconds


def _flush_verification_buffer(buffer, print_fn):
    text = buffer.strip()
    if text:
        print_fn(text)
    return ""


def _advance_or_stop(task_dir, current_path, print_fn, poll_interval, max_wait_seconds, waited):
    for _ in range(3):
        time.sleep(poll_interval)
        waited += poll_interval
        if _timed_out(waited, max_wait_seconds):
            print_fn("stopped watching (max wait reached); run may still be in progress")
            return "timed_out", "timed_out", waited
        located = locate(task_dir)
        if located is not None and located != current_path:
            return "advance", located, 0.0

    source = current_path.parent.name
    if source == "runs":
        try:
            state_obj = state.load_state(task_dir, task_dir.name)
            state.reconcile_artifacts(task_dir, state_obj, read_only=True)
        except CorruptState as exc:
            print_fn("pipeline state unreadable: %s" % exc)
            return "stop", "complete", 0.0
        state_name = state_obj.get("state")
        if state_name in ("complete", "failed", "blocked"):
            print_fn("pipeline finished: state=%s" % state_name)
            return "stop", "complete", 0.0
        if state_name in ("awaiting_human_test", "awaiting_final_decision", "awaiting_retry_approval"):
            print_fn("pipeline paused: state=%s" % state_name)
            return "stop", "complete", 0.0
        if state_name in ("running", "ready"):
            return "wait", None, waited
        return "wait", None, waited

    if source == "verification_runs":
        if (task_dir / "05_verification_report.md").exists():
            print_fn("verification complete")
            return "stop", "complete", 0.0
        return "wait", None, waited

    return "stop", "complete", 0.0


def follow(task_dir, stage=None, run_id=None, poll_interval=0.4, print_fn=print, max_wait_seconds=None, verbose=False):
    """Tail a run's stdout file, printing one summarized line per event as
    it arrives. Stops when the .json sidecar appears (run finished) or on
    KeyboardInterrupt. Returns a short status string."""
    stdout_path = locate(task_dir, stage, run_id)
    if stdout_path is None:
        print_fn("no runs found for this task yet")
        return "no_runs"

    print_fn("tailing %s" % stdout_path.name)
    unfiltered = stage is None and run_id is None
    waited = 0.0
    try:
        while True:
            source = stdout_path.parent.name
            agent = None
            buffer = ""
            with open(str(stdout_path), "r", encoding="utf-8", errors="replace") as handle:
                completed = False
                while True:
                    chunk = handle.read()
                    if chunk:
                        buffer += chunk
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line = line.strip()
                            if not line:
                                continue
                            if source == "verification_runs":
                                print_fn(line)
                                continue
                            try:
                                obj = json.loads(line)
                            except ValueError:
                                continue
                            if agent is None:
                                agent = stream_events.detect_agent(obj)
                            summary = stream_events.summarize_event(agent, obj, verbose=verbose)
                            if summary:
                                print_fn(summary)
                        waited = 0.0
                        continue
                    if _sidecar(stdout_path) is not None:
                        if source == "verification_runs":
                            buffer = _flush_verification_buffer(buffer, print_fn)
                        if not unfiltered:
                            print_fn("run complete")
                            return "complete"
                        completed = True
                        break
                    time.sleep(poll_interval)
                    waited += poll_interval
                    if _timed_out(waited, max_wait_seconds):
                        print_fn("stopped watching (max wait reached); run may still be in progress")
                        return "timed_out"

                while completed:
                    action, value, waited = _advance_or_stop(task_dir, stdout_path, print_fn, poll_interval, max_wait_seconds, waited)
                    if action == "advance":
                        stdout_path = value
                        print_fn("-- following %s" % stdout_path.name)
                        break
                    if action == "stop":
                        return value
                    if action == "timed_out":
                        return value
                    time.sleep(poll_interval)
                    waited += poll_interval
                    if _timed_out(waited, max_wait_seconds):
                        print_fn("stopped watching (max wait reached); run may still be in progress")
                        return "timed_out"
                continue
    except KeyboardInterrupt:
        print_fn("stopped watching; run may still be in progress")
        return "interrupted"


def _safe_read(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def brief(task_dir, stage=None, run_id=None, print_fn=print, verbose=False):
    """Print a compact summary of a run (in-progress or finished)."""
    stdout_path = _locate_pipeline_run(task_dir, stage, run_id)
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
                excerpt = reasoning_text if verbose else reasoning_text[:300] + ("..." if len(reasoning_text) > 300 else "")
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
        excerpt = text if verbose else text[:300] + ("..." if len(text) > 300 else "")
        print_fn("final text: " + excerpt.replace("\n", " "))
    return "ok"
