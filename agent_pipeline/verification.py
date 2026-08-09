"""Post-Stage-5 verification: build/test execution + structured evidence.

Standalone/human-triggered via `pipeline-verify`. Phase 3 of the
agent-pipeline redesign is expected to call `run_verification()` as a
library function when the overseer decides whether automated evidence is
sufficient to skip the human Stage 6 checkpoint -- this module only
produces and records the evidence, it does not act on it.

Reuses real_runner.run_to_files (the same subprocess-to-files primitive
invoke_agent uses for agent CLIs) for gradle/unittest invocations rather
than a third ad-hoc subprocess pattern.
"""

from __future__ import print_function

import json
import os
import re
import sys
import time
from pathlib import Path

from .locking import lock_path, pid_live
from .real_runner import run_to_files, write_json_atomic
from .state import orchestrator_dir


class VerificationError(Exception):
    pass


# run_unit_tests/run_mock_pipeline exercise this package's own code, not the
# driven project's -- they must run from the package's own install location
# (PACKAGE_ROOT), never from the driven project's repo_root.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent

GRADLE_JAVA_HOME_FALLBACK_ENV = "AGENT_PIPELINE_GRADLE_JAVA_HOME_FALLBACK"
GRADLE_ENV_DEFAULTS = {"JAVA_HOME": "/usr/lib/jvm/java-8-openjdk-amd64"}
UNIT_TEST_ARGS = ["-m", "unittest", "discover", "-s", "agent_pipeline/tests"]
MOCK_PIPELINE_ARGS = ["-m", "agent_pipeline.cli", "mock-test"]
TEST_PATH_MARKERS = ("src/test/", "tests/")
MANIFEST_VERIFICATION_KEYS = ("unit_tests", "mock_pipeline", "diff_check")
DRIVEN_PROJECT_DEFAULT_TIMEOUT_SECONDS = 600
DRIVEN_PROJECT_LAUNCH_FAILURE_EXIT_CODE = 126

_RAN_RE = re.compile(r"Ran (\d+) test")
_FAILED_COUNTS_RE = re.compile(r"FAILED \(([^)]*)\)")


def verification_runs_dir(task_dir):
    directory = orchestrator_dir(task_dir) / "verification_runs"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _write_check_sidecar(stdout_path, result):
    write_json_atomic(Path(stdout_path).with_suffix(".json"), result)


def _write_running_check_sidecar(stdout_path, name, argv, started_at):
    write_json_atomic(
        Path(stdout_path).with_suffix(".json"),
        {
            "name": name,
            "status": "running",
            "command": list(argv),
            "started_at": started_at,
            "stdout_path": str(stdout_path),
            "stderr_path": str(Path(stdout_path).with_suffix(".stderr")),
        },
    )


def check_concurrency_guard(task_dir, allow_pid=None):
    """Refuse to run while this task's own orchestrator lock is held by a
    live process -- a workspace-write stage (Stage 5) may be mid-edit, and
    a build/test run against the same working tree would race it.

    `allow_pid` lets the controller call this from inside its own
    already-locked pipeline-run (Stage 5 has already finished by the time
    Phase 3 calls verification, so there is no race with itself) without
    weakening the guard for the standalone `pipeline-verify` CLI path, which
    always passes allow_pid=None."""
    path = lock_path(task_dir)
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        raise VerificationError("task lock exists but is unreadable; refusing to verify while lock state is uncertain")
    if allow_pid is not None and data.get("pid") == allow_pid:
        return
    live = pid_live(data.get("pid"))
    if live is not False:
        raise VerificationError(
            "task lock is active (command=%s, pid=%s); refusing to run verification while a stage may be workspace-write in progress"
            % (data.get("command"), data.get("pid"))
        )


def run_unit_tests(repo_root, runs_dir, timeout_seconds=600):
    stamp = run_stamp()
    stdout_path = runs_dir / ("unit_tests-%s.stdout" % stamp)
    stderr_path = runs_dir / ("unit_tests-%s.stderr" % stamp)
    argv = [python_executable()] + UNIT_TEST_ARGS
    started = time.time()
    _write_running_check_sidecar(stdout_path, "unit_tests", argv, started)
    exit_code, timed_out = run_to_files(argv, stdout_path, stderr_path, timeout_seconds, cwd=PACKAGE_ROOT)
    duration = time.time() - started
    summary = parse_unittest_summary(safe_read(stderr_path))
    status = "passed" if exit_code == 0 and not timed_out else "failed"
    result = {
        "name": "unit_tests",
        "status": status,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_seconds": duration,
        "command": argv,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "summary": summary,
    }
    _write_check_sidecar(stdout_path, result)
    return result


def run_mock_pipeline(repo_root, runs_dir, timeout_seconds=120):
    stamp = run_stamp()
    stdout_path = runs_dir / ("mock_pipeline-%s.stdout" % stamp)
    stderr_path = runs_dir / ("mock_pipeline-%s.stderr" % stamp)
    argv = [python_executable()] + MOCK_PIPELINE_ARGS
    started = time.time()
    _write_running_check_sidecar(stdout_path, "mock_pipeline", argv, started)
    exit_code, timed_out = run_to_files(argv, stdout_path, stderr_path, timeout_seconds, cwd=PACKAGE_ROOT)
    duration = time.time() - started
    status = "passed" if exit_code == 0 and not timed_out else "failed"
    result = {
        "name": "mock_pipeline",
        "status": status,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_seconds": duration,
        "command": argv,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
    }
    _write_check_sidecar(stdout_path, result)
    return result


def run_gradle(repo_root, runs_dir, gradle_task, timeout_seconds=1800, env_overrides=None):
    gradlew = repo_root / "gradlew"
    if not gradlew.exists():
        return {"name": "gradle_" + gradle_task, "status": "not_attempted", "reason": "gradlew not found at repo root"}
    stamp = run_stamp()
    stdout_path = runs_dir / ("gradle_%s-%s.stdout" % (gradle_task, stamp))
    stderr_path = runs_dir / ("gradle_%s-%s.stderr" % (gradle_task, stamp))
    env = dict(os.environ)
    if not env.get("JAVA_HOME"):
        fallback = env.get(GRADLE_JAVA_HOME_FALLBACK_ENV)
        env["JAVA_HOME"] = fallback if fallback else GRADLE_ENV_DEFAULTS["JAVA_HOME"]
    gradle_home = repo_root / ".gradle-user-home"
    gradle_home.mkdir(parents=True, exist_ok=True)
    env["GRADLE_USER_HOME"] = str(gradle_home)
    if env_overrides:
        env.update(env_overrides)
    argv = [str(gradlew), "--no-daemon", gradle_task]
    started = time.time()
    _write_running_check_sidecar(stdout_path, "gradle_" + gradle_task, argv, started)
    exit_code, timed_out = run_to_files(argv, stdout_path, stderr_path, timeout_seconds, cwd=repo_root, env=env)
    duration = time.time() - started
    status = "passed" if exit_code == 0 and not timed_out else "failed"
    result = {
        "name": "gradle_" + gradle_task,
        "status": status,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_seconds": duration,
        "command": argv,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
    }
    _write_check_sidecar(stdout_path, result)
    return result


def run_driven_project_checks(repo_root, runs_dir, driven_project_commands=None):
    checks = []
    for command in driven_project_commands or []:
        name = command["name"]
        stamp = run_stamp()
        stdout_path = runs_dir / ("driven_project_%s-%s.stdout" % (name, stamp))
        stderr_path = runs_dir / ("driven_project_%s-%s.stderr" % (name, stamp))
        argv = list(command["argv"])
        timeout_seconds = command.get("timeout_seconds", DRIVEN_PROJECT_DEFAULT_TIMEOUT_SECONDS)
        started = time.time()
        _write_running_check_sidecar(stdout_path, "driven_project_" + name, argv, started)
        try:
            exit_code, timed_out = run_to_files(argv, stdout_path, stderr_path, timeout_seconds, cwd=repo_root)
        except OSError as exc:
            stdout_path.write_text("", encoding="utf-8")
            stderr_path.write_text(str(exc) + "\n", encoding="utf-8")
            exit_code = DRIVEN_PROJECT_LAUNCH_FAILURE_EXIT_CODE
            timed_out = False
        duration = time.time() - started
        status = "passed" if exit_code == 0 and not timed_out else "failed"
        result = {
            "name": "driven_project_" + name,
            "status": status,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "duration_seconds": duration,
            "command": argv,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        }
        _write_check_sidecar(stdout_path, result)
        checks.append(result)
    return checks


def is_test_path(path):
    return any(marker in path for marker in TEST_PATH_MARKERS)


def is_testable_source(path):
    return path.endswith(".py") or path.endswith(".java")


def test_coverage_delta_signal(manifest):
    """Detection-only signal: does the Stage 5 diff touch testable source
    without touching any test files? Never blocks by itself -- Phase 3 is
    where this becomes an enforced review criterion."""
    changed = [entry.get("path") for entry in (manifest or {}).get("changed_files", []) if entry.get("path")]
    if not changed:
        return {
            "status": "no_data",
            "touched_test_files": False,
            "testable_changed_paths": [],
            "flagged_paths": [],
            "note": "No changed_files recorded (no manifest, or Stage 5 made no file changes).",
        }
    touched_tests = any(is_test_path(p) for p in changed)
    testable = sorted(p for p in changed if is_testable_source(p) and not is_test_path(p))
    flagged = testable if (testable and not touched_tests) else []
    note = (
        "Diff touched testable source (%d file(s)) without touching a test path (%s) -- verify coverage manually."
        % (len(flagged), " or ".join(TEST_PATH_MARKERS))
        if flagged
        else "Test coverage looks proportionate to the diff (signal only, not enforced)."
    )
    return {
        "status": "flagged" if flagged else "ok",
        "touched_test_files": touched_tests,
        "testable_changed_paths": testable,
        "flagged_paths": flagged,
        "note": note,
    }


def load_manifest_if_present(task_dir):
    path = task_dir / "05_implementation_manifest.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def update_manifest_verification(task_dir, manifest, checks_by_name, coverage_signal):
    """Best-effort: fill in the manifest's long-standing 'not_attempted'
    verification placeholders with this run's real results. Only touches
    the verification/verification_evidence fields -- never changed_files
    or stage5_run, so it can't desync controller.py's provenance check."""
    if manifest is None:
        return None
    verification = dict(manifest.get("verification") or {})
    if "unit_tests" in checks_by_name:
        verification["unit_tests"] = checks_by_name["unit_tests"]["status"]
    if "mock_pipeline" in checks_by_name:
        verification["mock_pipeline"] = checks_by_name["mock_pipeline"]["status"]
    verification["diff_check"] = "failed" if coverage_signal["status"] == "flagged" else "passed"
    manifest["verification"] = verification
    evidence = list(manifest.get("verification_evidence") or [])
    for name, check in checks_by_name.items():
        evidence.append({"check": name, "status": check["status"], "recorded_at": now()})
    evidence.append({"check": "diff_check", "status": verification["diff_check"], "detail": coverage_signal["note"], "recorded_at": now()})
    manifest["verification_evidence"] = evidence
    path = task_dir / "05_implementation_manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def run_verification(
    task_dir,
    repo_root,
    run_build=False,
    unit_test_timeout=600,
    mock_pipeline_timeout=120,
    gradle_timeout=1800,
    allow_pid=None,
    driven_project_commands=None,
    skip_self_check=False,
    build_implies_compile=False,
):
    check_concurrency_guard(task_dir, allow_pid=allow_pid)
    runs_dir = verification_runs_dir(task_dir)

    checks = []
    if not skip_self_check:
        checks.extend([
            run_unit_tests(repo_root, runs_dir, timeout_seconds=unit_test_timeout),
            run_mock_pipeline(repo_root, runs_dir, timeout_seconds=mock_pipeline_timeout),
        ])
    if not (build_implies_compile and run_build):
        checks.append(run_gradle(repo_root, runs_dir, "compileJava", timeout_seconds=gradle_timeout))
    if run_build:
        checks.append(run_gradle(repo_root, runs_dir, "build", timeout_seconds=gradle_timeout))
    driven_project_checks = run_driven_project_checks(repo_root, runs_dir, driven_project_commands)
    checks.extend(driven_project_checks)
    driven_project_verified = bool(driven_project_checks) and all(check["status"] == "passed" for check in driven_project_checks)

    manifest = load_manifest_if_present(task_dir)
    coverage_signal = test_coverage_delta_signal(manifest)
    checks_by_name = {check["name"]: check for check in checks}
    updated_manifest = update_manifest_verification(task_dir, manifest, checks_by_name, coverage_signal)

    report = {
        "schema_version": 1,
        "generated_at": now(),
        "task": task_dir.name,
        "manifest_present": manifest is not None,
        "checks": checks,
        "driven_project_verified": driven_project_verified,
        "test_coverage_delta_signal": coverage_signal,
        "overall_status": overall_status(checks),
    }
    paths = write_report(task_dir, report)
    report["report_paths"] = paths
    report["manifest_updated"] = updated_manifest is not None
    return report


def overall_status(checks):
    statuses = [c.get("status") for c in checks]
    attempted = [s for s in statuses if s != "not_attempted"]
    if any(s == "failed" for s in attempted):
        return "failed"
    if attempted and all(s == "passed" for s in attempted):
        return "passed"
    return "incomplete"


def write_report(task_dir, report):
    json_path = task_dir / "05_verification_report.json"
    md_path = task_dir / "05_verification_report.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return {"json_path": str(json_path), "md_path": str(md_path)}


def render_markdown(report):
    lines = [
        "# Stage 5 verification report",
        "",
        "Generated: " + report.get("generated_at", ""),
        "Overall status: **%s**" % report.get("overall_status"),
        "Driven-project verified: **%s**" % str(bool(report.get("driven_project_verified"))).lower(),
        "",
        "## Checks",
        "",
    ]
    for check in report.get("checks", []):
        if "exit_code" in check:
            lines.append(
                "- **%s**: %s (exit=%s, %.1fs)"
                % (check.get("name"), check.get("status"), check.get("exit_code"), check.get("duration_seconds", 0.0))
            )
        else:
            lines.append("- **%s**: %s (%s)" % (check.get("name"), check.get("status"), check.get("reason", "")))
    signal = report.get("test_coverage_delta_signal", {})
    lines.extend(["", "## Test-coverage-delta signal", "", "Status: " + signal.get("status", "unknown"), "", signal.get("note", "")])
    if signal.get("flagged_paths"):
        lines.append("")
        lines.append("Flagged paths:")
        for path in signal["flagged_paths"]:
            lines.append("- " + path)
    return "\n".join(lines).rstrip() + "\n"


def parse_unittest_summary(stderr_text):
    text = stderr_text or ""
    ran_match = _RAN_RE.search(text)
    tests_run = int(ran_match.group(1)) if ran_match else None
    failed_match = _FAILED_COUNTS_RE.search(text)
    failures = errors = 0
    if failed_match:
        for part in failed_match.group(1).split(","):
            part = part.strip()
            if part.startswith("failures="):
                failures = int(part.split("=")[1])
            elif part.startswith("errors="):
                errors = int(part.split("=")[1])
    ok = failed_match is None and tests_run is not None
    return {"tests_run": tests_run, "ok": ok, "failures": failures, "errors": errors}


def python_executable():
    return sys.executable or "python3"


def run_stamp():
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + ("-%d" % (int(time.time() * 1000) % 1000))


def safe_read(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
