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
import socket
import subprocess
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
SOURCE_ROOT_EXCLUDES = set([".git", ".agent-pipeline", ".venv", "venv", "env", "build", "dist", "target", "__pycache__"])
GENERIC_SYMBOLS = set(["get", "set", "run", "main", "test", "init", "new", "old", "data", "value", "item"])

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
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "started_at": started_at,
            "stdout_path": str(stdout_path),
            "stderr_path": str(Path(stdout_path).with_suffix(".stderr")),
        },
    )


def check_concurrency_guard(task_dir, allow_pid=None):
    """Refuse to run while this task's own orchestrator lock is held by a
    live process -- a workspace-write stage (Stage 5) may be mid-edit, and
    a build/test run against the same working tree would race it.

    `allow_pid` lets a caller that already holds this task's own lock
    exempt itself without weakening the guard against any other live
    holder. Both callers pass their own pid: `pipeline_run`'s Stage 6
    auto-verify (Stage 5 has already finished by the time Phase 3 calls
    verification, so there is no race with itself), and the standalone
    `pipeline-verify` CLI path, which wraps itself in its own `TaskLock`
    before calling this and must exempt that same lock."""
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


def test_coverage_delta_signal(manifest, repo_root=None):
    """Detection-only signal: does the Stage 5 diff touch testable source
    without touching any test files? Never blocks by itself -- Phase 3 is
    where this becomes an enforced review criterion."""
    changed_entries = [entry for entry in (manifest or {}).get("changed_files", []) if entry.get("path")]
    changed = [entry.get("path") for entry in changed_entries]
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
    analysis_notes = []
    if testable and not touched_tests and repo_root is not None:
        changed_set = set(changed)
        entries_by_path = {}
        for entry in changed_entries:
            entries_by_path.setdefault(entry.get("path"), entry)
        flagged = []
        for path in testable:
            exempt, detail = _testable_path_exempt_from_coverage_flag(entries_by_path.get(path) or {"path": path}, Path(repo_root), changed_set)
            if not exempt:
                flagged.append(path)
            if detail:
                analysis_notes.append(detail)
        flagged = sorted(flagged)
    else:
        flagged = testable if (testable and not touched_tests) else []
    note = (
        "Diff touched testable source (%d file(s)) without touching a test path (%s) -- verify coverage manually."
        % (len(flagged), " or ".join(TEST_PATH_MARKERS))
        if flagged
        else "Test coverage looks proportionate to the diff (signal only, not enforced)."
    )
    if flagged and analysis_notes:
        limited = sorted(set(analysis_notes))
        note += " " + " ".join(limited[:3])
    return {
        "status": "flagged" if flagged else "ok",
        "touched_test_files": touched_tests,
        "testable_changed_paths": testable,
        "flagged_paths": flagged,
        "note": note,
    }


def _testable_path_exempt_from_coverage_flag(entry, repo_root, changed_paths):
    path = entry.get("path")
    reason = entry.get("reason")
    full = repo_root / path
    if reason == "reverted_to_clean_during_stage5":
        return True, None
    if reason == "deleted_during_stage5":
        if not full.exists():
            return True, None
        return False, "Some paths remained flagged because deletion evidence was ambiguous."
    if reason == "new_since_dirty_baseline":
        diff = _stage5_diff_for_new_since_baseline(repo_root, path)
        if diff is None:
            return False, "Some paths remained flagged because diff evidence was unavailable."
        return _exempt_from_diff_and_existing_tests(repo_root, path, diff, changed_paths)
    if reason in ("pre_dirty_hash_changed_during_stage5", "status_changed_after_stage5"):
        if not full.exists():
            return True, None
        if not _git_path_is_tracked(repo_root, path):
            return False, "Some paths remained flagged because diff evidence was unavailable."
        diff = _git_diff_head(repo_root, path)
        if diff is None:
            return False, "Some paths remained flagged because diff evidence was unavailable."
        if not _has_executable_delta(repo_root, path, diff):
            return True, None
        return False, "Some paths remained flagged because Stage 5 delta evidence was ambiguous."
    return False, "Some paths remained flagged because manifest reason evidence was ambiguous."


def _stage5_diff_for_new_since_baseline(repo_root, path):
    full = repo_root / path
    if _git_path_is_tracked(repo_root, path):
        return _git_diff_head(repo_root, path)
    if not full.exists() or not full.is_file():
        return None
    try:
        lines = full.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None
    header = ["diff --git a/%s b/%s" % (path, path), "--- /dev/null", "+++ b/" + path, "@@ -0,0 +1,%d @@" % len(lines)]
    return "\n".join(header + ["+" + line for line in lines]) + "\n"


def _git_path_is_tracked(repo_root, path):
    try:
        subprocess.check_output(["git", "ls-files", "--error-unmatch", "--", path], cwd=str(repo_root), stderr=subprocess.STDOUT)
        return True
    except Exception:
        return False


def _git_diff_head(repo_root, path):
    try:
        return subprocess.check_output(["git", "diff", "HEAD", "--", path], cwd=str(repo_root), stderr=subprocess.STDOUT).decode(
            "utf-8",
            "replace",
        )
    except Exception:
        return None


def _exempt_from_diff_and_existing_tests(repo_root, path, diff, changed_paths):
    if not _has_executable_delta(repo_root, path, diff):
        return True, None
    symbols = _changed_public_symbols(repo_root, path, diff)
    if symbols and _symbols_covered_by_untouched_tests(repo_root, symbols, changed_paths):
        return True, None
    return False, None


def _has_executable_delta(repo_root, path, diff):
    if not _diff_has_executable_additions(path, diff):
        return False
    current = _read_repo_text(repo_root, path)
    head = _git_show_head_file(repo_root, path)
    if current is not None and head is not None:
        if path.endswith(".py"):
            return _python_executable_text(current) != _python_executable_text(head)
        if path.endswith(".java"):
            return _strip_java_non_executable(current) != _strip_java_non_executable(head)
    return _diff_has_executable_additions(path, diff)


def _read_repo_text(repo_root, path):
    full = repo_root / path
    if not full.exists() or not full.is_file():
        return None
    try:
        return full.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def _git_show_head_file(repo_root, path):
    try:
        return subprocess.check_output(["git", "show", "HEAD:" + path], cwd=str(repo_root), stderr=subprocess.STDOUT).decode(
            "utf-8",
            "replace",
        )
    except Exception:
        return None


def _diff_has_executable_additions(path, diff):
    added = _diff_added_lines(diff)
    if not added:
        return False
    if path.endswith(".py"):
        return bool(_strip_python_non_executable("\n".join(added)).strip())
    if path.endswith(".java"):
        return bool(_strip_java_non_executable("\n".join(added)).strip())
    return True


def _diff_added_lines(diff):
    lines = []
    for line in (diff or "").splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            lines.append(line[1:])
    return lines


def _strip_python_non_executable(text):
    try:
        import io
        import tokenize
    except Exception:
        return text
    kept = []
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        for token in tokens:
            if token.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER):
                continue
            kept.append(token.string)
    except Exception:
        return "\n".join(line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#"))
    return " ".join(kept)


def _python_executable_text(text):
    without_docstrings = _blank_python_docstring_lines(text)
    return _strip_python_comments_and_layout(without_docstrings)


def _blank_python_docstring_lines(text):
    try:
        import ast
    except Exception:
        return text
    try:
        tree = ast.parse(text)
    except Exception:
        return text
    doc_lines = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        value = getattr(first, "value", None)
        if isinstance(first, ast.Expr) and isinstance(value, ast.Constant) and isinstance(value.value, str):
            end = getattr(first, "end_lineno", None) or getattr(first, "lineno", 0)
            for line_no in range(getattr(first, "lineno", 0), end + 1):
                doc_lines.add(line_no)
    lines = text.splitlines()
    return "\n".join("" if index in doc_lines else line for index, line in enumerate(lines, start=1))


def _strip_python_comments_and_layout(text):
    try:
        import io
        import tokenize
    except Exception:
        return text
    kept = []
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        for token in tokens:
            if token.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER):
                continue
            kept.append(token.string)
    except Exception:
        return "\n".join(line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#"))
    return " ".join(kept)


def _strip_java_non_executable(text):
    result = []
    i = 0
    in_block = False
    while i < len(text):
        if in_block:
            end = text.find("*/", i)
            if end == -1:
                break
            i = end + 2
            in_block = False
            continue
        if text.startswith("/*", i):
            in_block = True
            i += 2
            continue
        if text.startswith("//", i):
            newline = text.find("\n", i)
            if newline == -1:
                break
            i = newline + 1
            result.append("\n")
            continue
        result.append(text[i])
        i += 1
    return "\n".join(line for line in "".join(result).splitlines() if line.strip())


def _changed_public_symbols(repo_root, path, diff):
    full = repo_root / path
    if not full.exists() or not full.is_file():
        return []
    try:
        text = full.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    changed_lines = _diff_new_line_numbers(diff)
    if not changed_lines:
        return []
    if path.endswith(".py"):
        return _changed_python_symbols(text, changed_lines)
    if path.endswith(".java"):
        return _changed_java_symbols(text, changed_lines)
    return []


def _diff_new_line_numbers(diff):
    numbers = set()
    new_line = None
    for raw in (diff or "").splitlines():
        match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
        if match:
            new_line = int(match.group(1))
            continue
        if new_line is None or raw.startswith("\\"):
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            numbers.add(new_line)
            new_line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            continue
        else:
            new_line += 1
    return numbers


def _changed_python_symbols(text, changed_lines):
    try:
        import ast
    except Exception:
        return []
    try:
        tree = ast.parse(text)
    except Exception:
        return []
    symbols = []
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        name = getattr(node, "name", "")
        if _too_generic_symbol(name) or name.startswith("_"):
            continue
        end = getattr(node, "end_lineno", None) or getattr(node, "lineno", 0)
        if not any(getattr(node, "lineno", 0) <= line <= end for line in changed_lines):
            continue
        parent = parents.get(node)
        if isinstance(node, ast.ClassDef):
            symbols.append({"kind": "class", "name": name})
        elif isinstance(parent, ast.ClassDef):
            if not _too_generic_symbol(parent.name) and not parent.name.startswith("_"):
                symbols.append({"kind": "method", "name": name, "class": parent.name})
        else:
            symbols.append({"kind": "function", "name": name})
    return _dedupe_symbols(symbols)


def _changed_java_symbols(text, changed_lines):
    lines = text.splitlines()
    class_name = None
    symbols = []
    for index, line in enumerate(lines, start=1):
        class_match = re.search(r"\b(?:public\s+)?(?:final\s+|abstract\s+)?class\s+([A-Za-z_][A-Za-z0-9_]*)\b", line)
        if class_match:
            class_name = class_match.group(1)
            if index in changed_lines and not _too_generic_symbol(class_name):
                symbols.append({"kind": "class", "name": class_name})
        method_match = re.search(
            r"\b(?:public|protected)\s+(?:static\s+)?(?:final\s+)?[A-Za-z_][A-Za-z0-9_<>, ?\[\]]*\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
            line,
        )
        if method_match and any(abs(index - changed) <= 2 for changed in changed_lines):
            name = method_match.group(1)
            if class_name and not _too_generic_symbol(name):
                symbols.append({"kind": "method", "name": name, "class": class_name})
    return _dedupe_symbols(symbols)


def _too_generic_symbol(name):
    return not name or len(name) < 3 or name.lower() in GENERIC_SYMBOLS


def _dedupe_symbols(symbols):
    seen = set()
    result = []
    for symbol in symbols:
        key = tuple(sorted(symbol.items()))
        if key not in seen:
            seen.add(key)
            result.append(symbol)
    return result


def _symbols_covered_by_untouched_tests(repo_root, symbols, changed_paths):
    if not symbols:
        return False
    tests = _untouched_test_texts(repo_root, changed_paths)
    if not tests:
        return False
    for symbol in symbols:
        if not _symbol_has_test_evidence(symbol, tests):
            return False
    return True


def _untouched_test_texts(repo_root, changed_paths):
    texts = []
    for root, dirs, files in os.walk(str(repo_root)):
        dirs[:] = [d for d in dirs if d not in SOURCE_ROOT_EXCLUDES]
        root_path = Path(root)
        for filename in files:
            full = root_path / filename
            try:
                rel = full.relative_to(repo_root).as_posix()
            except ValueError:
                continue
            if rel in changed_paths or not is_test_path(rel):
                continue
            try:
                texts.append(full.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
    return texts


def _symbol_has_test_evidence(symbol, test_texts):
    for text in test_texts:
        if symbol["kind"] == "function" and _has_function_reference(text, symbol["name"]):
            return True
        if symbol["kind"] == "class" and _has_class_reference(text, symbol["name"]):
            return True
        if symbol["kind"] == "method" and _has_class_reference(text, symbol["class"]) and _has_token_reference(text, symbol["name"]):
            return True
    return False


def _has_function_reference(text, name):
    return bool(
        re.search(r"(?<![A-Za-z0-9_])%s\s*\(" % re.escape(name), text)
        or re.search(r"\bimport\s+%s\b" % re.escape(name), text)
    )


def _has_class_reference(text, name):
    return bool(_has_token_reference(text, name) or _has_token_reference(text, name + "Test") or _has_token_reference(text, "Test" + name))


def _has_token_reference(text, name):
    return bool(re.search(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(name), text))


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
    driven_project_status = driven_project_verification_status(driven_project_commands, driven_project_checks)

    manifest = load_manifest_if_present(task_dir)
    coverage_signal = test_coverage_delta_signal(manifest, repo_root=repo_root)
    checks_by_name = {check["name"]: check for check in checks}
    updated_manifest = update_manifest_verification(task_dir, manifest, checks_by_name, coverage_signal)

    report = {
        "schema_version": 1,
        "generated_at": now(),
        "task": task_dir.name,
        "manifest_present": manifest is not None,
        "checks": checks,
        "driven_project_checks_configured": driven_project_status["configured"],
        "driven_project_check_count": driven_project_status["configured_count"],
        "driven_project_verified": driven_project_verified,
        "driven_project_verification_reason": driven_project_status["reason"],
        "test_coverage_delta_signal": coverage_signal,
        "overall_status": overall_status(checks),
    }
    paths = write_report(task_dir, report)
    report["report_paths"] = paths
    report["manifest_updated"] = updated_manifest is not None
    return report


def driven_project_verification_status(driven_project_commands, driven_project_checks):
    configured_count = len(driven_project_commands or [])
    if configured_count == 0:
        return {
            "configured": False,
            "configured_count": 0,
            "reason": "no driven-project commands configured",
        }
    failed = [check.get("name") for check in driven_project_checks if check.get("status") == "failed"]
    if failed:
        return {
            "configured": True,
            "configured_count": configured_count,
            "reason": "configured driven-project command failed: " + ", ".join(failed),
        }
    incomplete = [check.get("name") for check in driven_project_checks if check.get("status") != "passed"]
    if incomplete:
        return {
            "configured": True,
            "configured_count": configured_count,
            "reason": "configured driven-project command did not pass: " + ", ".join(incomplete),
        }
    return {
        "configured": True,
        "configured_count": configured_count,
        "reason": "all configured driven-project commands passed",
    }


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
        "Driven-project checks configured: **%s** (%d)" % (
            str(bool(report.get("driven_project_checks_configured"))).lower(),
            int(report.get("driven_project_check_count") or 0),
        ),
        "Driven-project verification reason: %s" % report.get("driven_project_verification_reason", "unknown"),
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
