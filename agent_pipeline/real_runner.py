"""Real subprocess adapters for configured agent CLIs."""

from __future__ import print_function

import os
import errno
import signal
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

from . import stream_events
from . import usage as usage_module
from .config import agent_config
from .failures import (
    FAILURE_CLASS_MAX_TURNS,
    FAILURE_CLASS_PERMISSION_ERROR,
    FAILURE_CLASS_PROCESS_INTERRUPTED,
    FAILURE_CLASS_SANDBOX_ENVIRONMENT,
    FAILURE_CLASS_SOURCE_FAILURE,
    FAILURE_CLASS_TIMEOUT,
    FAILURE_CLASS_UNKNOWN_FAILURE,
)
from .state import orchestrator_dir
from .durable import atomic_write_json
from .locking import process_group_live, process_start_identity, record_managed_child, record_managed_child_launch_state


class RealRunnerError(Exception):
    pass


class ManagedProcessInterrupted(Exception):
    def __init__(self, signum):
        Exception.__init__(self, "managed process interrupted by signal %s" % signum)
        self.signum = signum
        self.result = None


def invoke_agent(
    task_dir,
    config,
    agent,
    stage_key,
    execution_mode,
    prompt_path,
    candidate_path,
    run_id,
    pass_number=1,
    attempt_number=1,
    attempt_kind="normal",
    retry_reason="initial/no-retry",
    task=None,
    ledger_path=None,
    capture_reasoning=True,
    repo_root=None,
):
    runs_dir = orchestrator_dir(task_dir) / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = Path(candidate_path)
    if candidate_path.name.endswith(".candidate.md"):
        base = candidate_path.name[: -len(".candidate.md")]
    else:
        base = "%s-pass-%s-attempt-%s-%s-%s" % (stage_key, pass_number, attempt_number, agent, run_id)
    stdout_path = runs_dir / (base + ".stdout")
    stderr_path = runs_dir / (base + ".stderr")
    metadata_path = runs_dir / (base + ".json")
    candidate_path.parent.mkdir(parents=True, exist_ok=True)

    detail = dict(agent_config(config, agent))
    role = config.get("roles", {}).get(stage_key, {})
    if role.get("model_override"):
        detail["model"] = role.get("model_override")
    if role.get("effort_override"):
        detail["read_effort"] = role.get("effort_override")
        detail["write_effort"] = role.get("effort_override")
    started = now()
    started_monotonic = time.time()
    argv = None
    metadata_argv = None
    exit_code = None
    failure_class = None
    partial = False
    real_process_invoked = False
    child_identity = None
    interruption = None

    running_record = {
        "agent": agent,
        "provider": agent,
        "stage": stage_key,
        "execution_mode": execution_mode,
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "started_at": started,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "run_id": run_id,
        "pass_number": pass_number,
        "attempt_number": attempt_number,
        "attempt_kind": attempt_kind,
        "retry_reason": retry_reason,
        "status": "running",
    }

    def mark_launched(identity=None):
        nonlocal real_process_invoked, child_identity
        real_process_invoked = True
        if identity is None:
            return
        child_identity = dict(identity)
        launched_record = dict(running_record)
        launched_record["managed_child"] = child_identity
        write_json_atomic(metadata_path, launched_record)
        if repo_root is not None:
            record_managed_child(task_dir, repo_root, run_id, child_identity)

    def mark_launch_pending():
        if repo_root is not None:
            record_managed_child_launch_state(task_dir, repo_root, run_id, "launch_pending")

    def mark_launch_failed():
        if repo_root is not None:
            record_managed_child_launch_state(task_dir, repo_root, run_id, "launch_failed")

    write_json_atomic(metadata_path, running_record)

    try:
        argv, metadata_argv = build_argv(agent, detail, execution_mode, prompt_path, candidate_path, config, stage_key)
        if not command_available(argv[0]):
            raise RealRunnerError("provider command not found: " + argv[0])
        prompt_text = Path(prompt_path).read_text(encoding="utf-8")
        stdin_text = prompt_text if agent in ("codex", "agy") and uses_stdin(agent, argv) else None
        try:
            exit_code, timed_out = run_to_files(
                argv,
                stdout_path,
                stderr_path,
                int(config.get("timeout_seconds", 3600)),
                stdin_text=stdin_text,
                on_launch_identity=mark_launched,
                on_before_launch=mark_launch_pending,
                on_launch_failed=mark_launch_failed,
                run_id=run_id,
            )
            if timed_out:
                failure_class = FAILURE_CLASS_TIMEOUT
        except ManagedProcessInterrupted as exc:
            interruption = exc
            exit_code = -int(exc.signum)
            failure_class = FAILURE_CLASS_PROCESS_INTERRUPTED
    except RealRunnerError as exc:
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text(str(exc) + "\n", encoding="utf-8")
        exit_code = 127
        failure_class = FAILURE_CLASS_SOURCE_FAILURE
    except OSError as exc:
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text(str(exc) + "\n", encoding="utf-8")
        if getattr(exc, "errno", None) in (errno.EACCES, errno.EPERM):
            exit_code = 126
            failure_class = FAILURE_CLASS_PERMISSION_ERROR
        else:
            exit_code = 127
            failure_class = FAILURE_CLASS_SOURCE_FAILURE

    ended = now()
    duration = time.time() - started_monotonic
    stdout_text = safe_read(stdout_path)
    stderr_text = safe_read(stderr_path)
    events = stream_events.parse_json_lines(stdout_text)
    if failure_class is None:
        failure_class = classify(exit_code, stdout_text, stderr_text, agent, events=events)
    reset_at = stream_events.reset_at(agent, stdout_text, events=events)
    if failure_class in (FAILURE_CLASS_MAX_TURNS, FAILURE_CLASS_PROCESS_INTERRUPTED, FAILURE_CLASS_TIMEOUT):
        partial = True

    extracted_path = extract_candidate(candidate_path, stdout_text, agent, events=events)
    usage_data = stream_events.usage_summary(agent, stdout_text, events=events)
    if isinstance(usage_data, dict):
        if agent == "codex":
            usage_data["total_cost_usd_estimated"] = usage_module.estimate_cost_usd(
                usage_data,
                detail.get("model"),
                config.get("pricing", {}).get("codex", {}),
            )
        else:
            usage_data["total_cost_usd_estimated"] = None
    reasoning_text = stream_events.reasoning_summary(agent, stdout_text, events=events)
    reasoning_path = None
    if reasoning_text and capture_reasoning:
        reasoning_path = runs_dir / (base + ".reasoning.md")
        reasoning_path.write_text(
            "# Reasoning trace — stage %s, %s, run %s\n\n%s\n" % (stage_key, agent, run_id, reasoning_text),
            encoding="utf-8",
        )
    result = {
        "agent": agent,
        "provider": agent,
        "model": detail.get("model"),
        "stage": stage_key,
        "execution_mode": execution_mode,
        "command_argv": metadata_argv or argv or [],
        "started_at": started,
        "ended_at": ended,
        "duration_seconds": duration,
        "exit_code": exit_code,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "candidate_artifact_path": str(extracted_path),
        "turn_budget": int(config.get("turn_budgets", {}).get(stage_key, 20)),
        "failure_class": failure_class,
        "reset_at": reset_at,
        "partial": partial,
        "real_process_invoked": real_process_invoked,
        "run_id": run_id,
        "pass_number": pass_number,
        "attempt_number": attempt_number,
        "attempt_kind": attempt_kind,
        "retry_reason": retry_reason,
        "status": "interrupted" if interruption is not None else ("passed" if exit_code == 0 and failure_class is None else "failed"),
        "usage": usage_data,
        "reasoning_path": str(reasoning_path) if reasoning_path else None,
    }
    if child_identity is not None:
        result["managed_child"] = child_identity
    write_json_atomic(metadata_path, result)
    result["metadata_path"] = str(metadata_path)
    if ledger_path is not None:
        entry = usage_module.build_entry(task, run_id, stage_key, agent, result, usage_data)
        usage_module.append_entry(ledger_path, entry)
    if interruption is not None:
        interruption.result = result
        raise interruption
    return result


def write_json_atomic(path, data):
    atomic_write_json(path, data)


def run_to_files(argv, stdout_path, stderr_path, timeout_seconds, stdin_text=None, cwd=None, env=None, on_launch=None, run_id=None, on_launch_identity=None, on_before_launch=None, on_launch_failed=None):
    """Run argv to completion, writing stdout/stderr to files as they're
    produced. Shared by invoke_agent (agent CLIs) and verification.py
    (gradle/unittest) so there's one subprocess-invocation pattern, not a
    third ad-hoc one. Returns (exit_code, timed_out). Timeout and controller
    interruption first request graceful group shutdown, then force and reap."""
    with open(str(stdout_path), "wb") as stdout_handle:
        with open(str(stderr_path), "wb") as stderr_handle:
            if on_before_launch is not None:
                on_before_launch()
            try:
                process = subprocess.Popen(
                    argv,
                    stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    cwd=str(cwd) if cwd is not None else None,
                    env=env,
                    start_new_session=True,
                )
            except BaseException:
                if on_launch_failed is not None:
                    on_launch_failed()
                raise
            child_identity = {
                "pid": process.pid,
                # start_new_session=True makes the child the new group leader.
                "pgid": process.pid,
                "host": socket.gethostname(),
                "process_start_identity": process_start_identity(process.pid),
                "run_id": run_id,
            }
            old_handlers = _install_interruption_handlers()
            try:
                if on_launch_identity is not None:
                    on_launch_identity(child_identity)
                elif on_launch is not None:
                    on_launch()
                try:
                    process.communicate(
                        stdin_text.encode("utf-8") if stdin_text is not None else None,
                        timeout=timeout_seconds,
                    )
                    return process.returncode, False
                except subprocess.TimeoutExpired:
                    _terminate_and_reap(process, child_identity["pgid"])
                    return -1, True
                except ManagedProcessInterrupted:
                    _terminate_and_reap(process, child_identity["pgid"])
                    raise
                except KeyboardInterrupt:
                    _terminate_and_reap(process, child_identity["pgid"])
                    raise ManagedProcessInterrupted(signal.SIGINT)
            except BaseException:
                _terminate_and_reap(process, child_identity["pgid"])
                raise
            finally:
                _restore_interruption_handlers(old_handlers)


def _signal_interruption(signum, frame):
    raise ManagedProcessInterrupted(signum)


def _install_interruption_handlers():
    if threading.current_thread() is not threading.main_thread():
        return None
    old_handlers = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        old_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, _signal_interruption)
    return old_handlers


def _restore_interruption_handlers(old_handlers):
    if not old_handlers:
        return
    for signum, handler in old_handlers.items():
        signal.signal(signum, handler)


def _terminate_and_reap(process, pgid, grace_seconds=5.0):
    """Terminate the owned group and always reap its direct child."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except Exception:
        try:
            process.terminate()
        except Exception:
            pass

    deadline = time.monotonic() + min(5.0, max(0.0, float(grace_seconds)))
    while time.monotonic() < deadline:
        process.poll()
        group_live = process_group_live(pgid)
        if group_live is False:
            break
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    if process_group_live(pgid) is not False:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
    try:
        process.communicate()
    except Exception:
        try:
            process.wait()
        except Exception:
            pass


def build_argv(agent, detail, execution_mode, prompt_path, candidate_path, config, stage_key):
    command = detail.get("command") or agent
    turn_budget = str(config.get("turn_budgets", {}).get(stage_key, 20))
    if execution_mode == "read-only" and detail.get("read_only", True) is not True:
        raise RealRunnerError("provider does not support the configured read-only execution mode")
    if agent == "codex":
        sandbox = "workspace-write" if execution_mode == "workspace-write" else "read-only"
        extra = detail.get("write_args" if execution_mode == "workspace-write" else "read_args", [])
        argv = [command, "exec", "--ephemeral", "--json", "--sandbox", sandbox] + list(extra)
        if detail.get("model"):
            argv += ["--model", str(detail["model"])]
        argv += ["--output-last-message", str(candidate_path), "-"]
        return argv, list(argv)
    if agent == "claude":
        permission_mode = "acceptEdits" if execution_mode == "workspace-write" else "plan"
        effort = detail.get("write_effort" if execution_mode == "workspace-write" else "read_effort", "medium")
        extra = detail.get("write_args" if execution_mode == "workspace-write" else "read_args", [])
        prompt_text = Path(prompt_path).read_text(encoding="utf-8")
        argv = [
            command,
            "-p",
            "--no-session-persistence",
            "--permission-mode",
            permission_mode,
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--verbose",
        ]
        if detail.get("model"):
            argv += ["--model", str(detail["model"])]
        argv += ["--effort", str(effort), "--max-turns", turn_budget]
        argv += list(extra)
        argv += [prompt_text]
        metadata = list(argv)
        metadata[-1] = "<prompt-text:%s>" % prompt_path
        return argv, metadata
    if agent == "agy":
        prompt_mode = detect_agy_prompt_mode(command, detail)
        if prompt_mode == "stdin" and not detail.get("stdin_mode_allowed"):
            raise RealRunnerError("Antigravity stdin mode requires explicit config")
        if execution_mode == "workspace-write" and not detail.get("workspace_write"):
            raise RealRunnerError("Antigravity workspace-write capability is not enabled")
        extra = detail.get("write_args" if execution_mode == "workspace-write" else "read_args", [])
        argv = [command, "--output-format", "stream-json"] + list(detail.get("common_args", [])) + list(extra)
        prompt_text = Path(prompt_path).read_text(encoding="utf-8")
        if prompt_mode == "print":
            argv += ["-p", prompt_text]
            metadata = list(argv)
            metadata[-1] = "<prompt-text:%s>" % prompt_path
            return argv, metadata
        if prompt_mode == "prompt":
            argv += ["--prompt", prompt_text]
            metadata = list(argv)
            metadata[-1] = "<prompt-text:%s>" % prompt_path
            return argv, metadata
        if prompt_mode == "stdin":
            return argv, list(argv) + ["<", str(prompt_path)]
        raise RealRunnerError("unsupported Antigravity prompt mode: " + str(prompt_mode))
    raise RealRunnerError("unsupported agent: " + str(agent))


def detect_agy_prompt_mode(command, detail):
    mode = detail.get("prompt_mode", "auto")
    if mode != "auto":
        return mode
    if not command_available(command):
        raise RealRunnerError("provider command not found: " + command)
    try:
        output = subprocess.check_output([command, "--help"], stderr=subprocess.STDOUT, timeout=10)
        text = output.decode("utf-8", "replace")
    except Exception:
        text = ""
    if " --print" in text or "\n  -p" in text or ", -p" in text:
        return "print"
    if "--prompt" in text:
        return "prompt"
    return "stdin"


def uses_stdin(agent, argv):
    if agent == "codex":
        return True
    if agent == "agy":
        return "-p" not in argv and "--prompt" not in argv
    return False


def command_available(command):
    if os.path.sep in command:
        return os.path.exists(command) and os.access(command, os.X_OK)
    return shutil.which(command) is not None


def classify(exit_code, stdout_text, stderr_text, agent=None, events=None):
    structured = stream_events.structured_failure(agent, stdout_text, events=events)
    if structured:
        return structured
    if exit_code == 0:
        return None
    if exit_code in (130, -2, -15):
        return FAILURE_CLASS_PROCESS_INTERRUPTED
    if exit_code == -1:
        return FAILURE_CLASS_TIMEOUT
    fallback = stderr_text.lower()
    if "max turns" in fallback or "maximum turns" in fallback or "turn limit" in fallback:
        return FAILURE_CLASS_MAX_TURNS
    usage_or_rate = stream_events.usage_or_rate_failure(fallback)
    if usage_or_rate:
        return usage_or_rate
    if "permission denied" in fallback or "operation not permitted" in fallback:
        return FAILURE_CLASS_PERMISSION_ERROR
    if "sandbox" in fallback:
        return FAILURE_CLASS_SANDBOX_ENVIRONMENT
    return FAILURE_CLASS_UNKNOWN_FAILURE


def extract_candidate(configured_path, stdout_text, agent=None, events=None):
    configured_path = Path(configured_path)
    if configured_path.exists() and configured_path.stat().st_size > 0:
        return configured_path
    text = stream_events.final_text(agent, stdout_text, events=events)
    configured_path.write_text(text if text is not None else stdout_text, encoding="utf-8")
    return configured_path


def safe_read(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
