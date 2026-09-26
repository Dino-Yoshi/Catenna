"""R07 regressions: snapshot consistency and actionable stale reasons (D8).

Capture tests hash a real temporary Git worktree and mutate real files at
the exact moment they are being read. Pipeline tests reuse the C06 fixture:
the real controller and real source-identity capture with fake agent CLIs.
"""

from __future__ import print_function

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_pipeline import controller
from agent_pipeline import source_identity
from agent_pipeline import verification
from agent_pipeline.failures import EXIT_BLOCKED, EXIT_SUCCESS
from agent_pipeline.source_identity import (
    MAX_REPORTED_PATHS,
    UNTRACKED_OUTPUT_HINT,
    SourceIdentityError,
    capture_source_identity,
    compute_fingerprint,
    describe_identity_change,
    identity_problem,
    with_identity_change,
)
from agent_pipeline.state import CONTRACTS, orchestrator_dir
from agent_pipeline.tests import test_c06_evidence as c06


def git(root, *args):
    subprocess.check_call(
        ["git", "-c", "user.name=Catenna Test", "-c", "user.email=test@example.invalid"] + list(args),
        cwd=str(root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def patch(test, owner, name, value):
    original = getattr(owner, name)
    setattr(owner, name, value)
    test.addCleanup(setattr, owner, name, original)
    return original


def count_capture_attempts(test):
    """Wrap one capture attempt; returns the list of attempt outcomes."""
    outcomes = []
    original = source_identity._capture_once

    def counted(*args, **kwargs):
        try:
            identity = original(*args, **kwargs)
        except SourceIdentityError as exc:
            outcomes.append(type(exc).__name__)
            raise
        outcomes.append("ok")
        return identity

    patch(test, source_identity, "_capture_once", counted)
    return outcomes


def mutate_while_hashing(test, trigger, action, times):
    """While `trigger` is being read (after its bytes are hashed, before the
    read completes), run `action` up to `times` times."""
    original = source_identity._hash_stream
    fired = []

    def racing(handle):
        digest = original(handle)
        try:
            same = os.fstat(handle.fileno()).st_ino == os.stat(str(trigger)).st_ino
        except OSError:
            same = False
        if same and len(fired) < times:
            fired.append(1)
            action()
        return digest

    patch(test, source_identity, "_hash_stream", racing)
    return fired


class R07CaptureConsistencyTests(unittest.TestCase):
    """R07-FR1: content changes to hashed entries during a capture are
    detected; the capture retries, then raises SourceIdentityError."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        git(self.root, "init", "-q")
        self.app = self.root / "app.py"
        self.app.write_text("print('v1')\n", encoding="utf-8")
        (self.root / "lib.py").write_text("X = 1\n", encoding="utf-8")
        self.zeta = self.root / "zeta.py"
        self.zeta.write_text("Z = 1\n", encoding="utf-8")
        git(self.root, "add", ".")
        git(self.root, "commit", "-q", "-m", "base")

    def append_to_app(self):
        with open(str(self.app), "a", encoding="utf-8") as handle:
            handle.write("print('more')\n")

    def settled(self):
        """A capture of the tree as it is now, with no interference."""
        return capture_source_identity(self.root)["fingerprint"]

    def test_fr1_file_mutated_while_hashed_is_retried_and_matches_one_state(self):
        before = self.settled()
        attempts = count_capture_attempts(self)
        fired = mutate_while_hashing(self, self.app, self.append_to_app, times=1)

        identity = capture_source_identity(self.root)

        self.assertEqual(fired, [1])
        self.assertEqual(attempts, ["_ContentChanged", "ok"])
        self.assertIsNone(identity_problem(identity))
        # The accepted identity describes the settled new state, not a mix.
        self.assertNotEqual(identity["fingerprint"], before)
        self.assertEqual(identity["fingerprint"], self.settled())

    def test_fr1_file_mutated_on_every_attempt_raises_after_three_attempts(self):
        attempts = count_capture_attempts(self)
        mutate_while_hashing(self, self.app, self.append_to_app, times=99)

        with self.assertRaises(SourceIdentityError) as caught:
            capture_source_identity(self.root)

        self.assertEqual(attempts, ["_ContentChanged"] * source_identity.CAPTURE_ATTEMPTS)
        self.assertEqual(source_identity.CAPTURE_ATTEMPTS, 3)
        self.assertIn("3 attempts", str(caught.exception))
        self.assertIn("app.py", str(caught.exception))

    def test_fr1_already_hashed_file_changed_later_in_capture_is_detected(self):
        # app.py is hashed before zeta.py; changing it while zeta.py is read
        # would otherwise yield an identity matching neither tree state.
        attempts = count_capture_attempts(self)
        mutate_while_hashing(self, self.zeta, self.append_to_app, times=1)

        identity = capture_source_identity(self.root)

        self.assertEqual(attempts, ["_ContentChanged", "ok"])
        self.assertEqual(identity["fingerprint"], self.settled())

    def test_fr1_file_replaced_while_hashed_is_detected(self):
        replacement = self.root / "replacement.tmp"

        def replace():
            replacement.write_text("print('v9')\n", encoding="utf-8")
            os.replace(str(replacement), str(self.app))

        attempts = count_capture_attempts(self)
        mutate_while_hashing(self, self.app, replace, times=1)

        identity = capture_source_identity(self.root)

        self.assertEqual(attempts, ["_ContentChanged", "ok"])
        self.assertEqual(identity["fingerprint"], self.settled())

    def test_fr1_same_size_rewrite_hidden_by_coarse_timestamps_is_detected(self):
        before = self.settled()
        original_key = source_identity._stat_key

        def coarse(info):
            # Simulate a filesystem whose timestamps do not move for a write
            # within the same tick: drop mtime/ctime from the signature.
            key = original_key(info)
            return key[:5] if len(key) == 7 else key

        patch(self, source_identity, "_stat_key", coarse)

        def rewrite_same_size():
            with open(str(self.app), "r+", encoding="utf-8") as handle:
                handle.write("print('v2')\n")

        attempts = count_capture_attempts(self)
        mutate_while_hashing(self, self.app, rewrite_same_size, times=1)

        identity = capture_source_identity(self.root)

        self.assertEqual(attempts, ["_ContentChanged", "ok"])
        self.assertNotEqual(identity["fingerprint"], before)
        self.assertEqual(identity["fingerprint"], self.settled())

    def test_fr1_unchanged_tree_captures_in_one_attempt(self):
        attempts = count_capture_attempts(self)
        first = capture_source_identity(self.root)
        second = capture_source_identity(self.root)
        self.assertEqual(attempts, ["ok", "ok"])
        self.assertEqual(first["fingerprint"], second["fingerprint"])

    def test_constraint_fingerprint_schema_is_unchanged(self):
        # Pinned from the pre-R07 implementation: existing recorded identities
        # must stay comparable with new captures.
        repo = Path(self.tmp.name) / "pinned"
        repo.mkdir()
        git(repo, "init", "-q")
        for name, text in (("app.py", "print('v1')\n"), ("lib.py", "X = 1\n"), (".gitignore", "build/\n")):
            (repo / name).write_text(text, encoding="utf-8")
            os.chmod(str(repo / name), 0o644)
        git(repo, "add", ".")
        (repo / "notes.txt").write_text("new\n", encoding="utf-8")
        os.chmod(str(repo / "notes.txt"), 0o644)

        identity = capture_source_identity(repo)

        self.assertIsNone(identity["head"])
        self.assertEqual(identity["fingerprint"], "b86ee557f6fdffddbd568d1cbf37187dbf5dd8da4f850dbfebe3e263187f1192")
        self.assertEqual(
            sorted(identity),
            ["captured_at", "fingerprint", "head", "index", "path_fingerprints", "schema", "tracked_count", "untracked", "untracked_count", "worktree"],
        )
        stripped = {key: identity[key] for key in ("schema", "head", "index", "worktree", "untracked")}
        self.assertEqual(compute_fingerprint(stripped), identity["fingerprint"])


class R07ReasonFormattingTests(unittest.TestCase):
    """R07-FR2/FR3 on real captures: changed paths, the 20-path cap, and the
    untracked-output hint."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        git(self.root, "init", "-q")
        (self.root / "app.py").write_text("x = 1\n", encoding="utf-8")
        git(self.root, "add", ".")
        git(self.root, "commit", "-q", "-m", "base")
        self.before = capture_source_identity(self.root)

    def test_fr2_tracked_change_names_path_without_hint(self):
        (self.root / "app.py").write_text("x = 2\n", encoding="utf-8")
        text = describe_identity_change(self.before, capture_source_identity(self.root))
        self.assertEqual(text, "changed paths: worktree:app.py")
        self.assertNotIn(UNTRACKED_OUTPUT_HINT, text)

    def test_fr2_fr3_lists_at_most_twenty_paths_then_remaining_count(self):
        (self.root / "gen").mkdir()
        for index in range(MAX_REPORTED_PATHS + 5):
            (self.root / "gen" / ("f%02d.py" % index)).write_text("", encoding="utf-8")
        after = capture_source_identity(self.root)

        text = describe_identity_change(self.before, after)

        self.assertEqual(MAX_REPORTED_PATHS, 20)
        for index in range(20):
            self.assertIn("untracked:gen/f%02d.py" % index, text)
        self.assertNotIn("gen/f20.py", text)
        self.assertIn("(and 5 more)", text)
        self.assertIn(UNTRACKED_OUTPUT_HINT, text)
        self.assertEqual(
            with_identity_change("source identity changed", self.before, after),
            "source identity changed (%s)" % text,
        )

    def test_fr2_incomplete_identity_leaves_reason_unchanged(self):
        (self.root / "app.py").write_text("x = 2\n", encoding="utf-8")
        after = capture_source_identity(self.root)
        broken = dict(self.before, fingerprint="0" * 64)
        self.assertEqual(describe_identity_change(broken, after), "")
        self.assertEqual(with_identity_change("stale", broken, after), "stale")
        self.assertEqual(with_identity_change("stale", None, after), "stale")


class R07VerificationCheckOutputTests(unittest.TestCase):
    """R07 acceptance with a real check subprocess writing `__pycache__`."""

    WRITE_PYCACHE = (
        "import os; os.makedirs('__pycache__', exist_ok=True); "
        "open('__pycache__/app.cpython-3.pyc', 'wb').write(b'x')"
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        subprocess.check_call(["git", "init", "-q"], cwd=str(self.repo))
        (self.repo / "app.py").write_text("x = 1\n", encoding="utf-8")
        self.task_dir = self.repo / ".agent-pipeline" / "tasks" / "t"
        self.task_dir.mkdir(parents=True)

    def verify(self):
        return verification.run_verification(
            self.task_dir,
            self.repo,
            skip_self_check=True,
            driven_project_commands=[{"name": "check", "argv": [sys.executable, "-c", self.WRITE_PYCACHE]}],
        )

    def test_fr2_fr3_unignored_check_output_names_path_and_ignore_hint(self):
        binding = self.verify()["source_identity"]

        self.assertFalse(binding["current"])
        self.assertIn("changed while checks ran", binding["reason"])
        self.assertIn("untracked:__pycache__/app.cpython-3.pyc", binding["reason"])
        self.assertIn(UNTRACKED_OUTPUT_HINT, binding["reason"])

    def test_fr3_ignored_check_output_keeps_verification_current(self):
        (self.repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        binding = self.verify()["source_identity"]
        self.assertTrue(binding["current"])
        self.assertNotIn("changed paths", binding["reason"])


class R07PipelineReasonTests(c06.C06EvidenceTests):
    """R07-FR1..FR3 through the real controller (verification, Stage 6-8)."""

    def pycache(self, name="app.cpython-3.pyc"):
        directory = self.root / "__pycache__"
        directory.mkdir(exist_ok=True)
        (directory / name).write_bytes(b"x")
        return "untracked:__pycache__/" + name

    def evidence_lines(self, status):
        return {
            line.split(":", 1)[0]: line
            for line in status.splitlines()
            if line.split(":", 1)[0] in ("verification", "stage06_evidence", "stage07_evidence", "stage08_evidence", "final_decision_status")
        }

    def test_r07_valid_automatic_acceptance_is_unaffected(self):
        self.start()
        attempts = count_capture_attempts(self)

        state = self.accept()

        self.assertTrue(attempts)
        self.assertEqual(set(attempts), {"ok"})
        record = state["evidence"]["verification"][-1]
        self.assertTrue(record["source_current"])
        self.assertEqual(record["source_reason"], "source identity unchanged across verification")
        self.assertNotIn("changed paths", self.status_output())

    def test_r07_fr1_capture_that_keeps_changing_leaves_evidence_unverified(self):
        self.start()
        self.accept()
        victim = self.root / "busy.py"
        victim.write_text("n = 0\n", encoding="utf-8")
        self.git("add", "busy.py")
        self.git("commit", "-q", "-m", "busy")

        def append():
            with open(str(victim), "a", encoding="utf-8") as handle:
                handle.write("n += 1\n")

        mutate_while_hashing(self, victim, append, times=10 ** 6)

        self.assertEqual(self.pipeline(), EXIT_BLOCKED)
        self.assertIn("kept changing", self.state()["last_failure"]["reason"])
        status = self.status_output()
        self.assertIn("source_identity: unavailable", status)
        self.assertIn("current_acceptance: no", status)

    def test_r07_fr2_fr3_check_output_during_verification_names_path_and_hint(self):
        self.start()
        written = []
        self.during_verification = lambda: written.append(self.pycache())

        self.assertEqual(self.pipeline(), EXIT_BLOCKED)

        reason = self.state()["human_checkpoint"]["reason"]
        self.assertIn("changed during verification", reason)
        self.assertIn(written[0], reason)
        self.assertIn(UNTRACKED_OUTPUT_HINT, reason)
        verification_line = self.evidence_lines(self.status_output())["verification"]
        self.assertIn(written[0], verification_line)
        self.assertIn(UNTRACKED_OUTPUT_HINT, verification_line)

    def test_r07_fr2_tracked_drift_names_path_in_stage6_7_8_reasons(self):
        (self.root / "app.py").write_text("print('v1')\n", encoding="utf-8")
        self.git("add", "app.py", ".gitignore")
        self.git("commit", "-q", "-m", "base")
        self.start()
        self.accept()

        (self.root / "app.py").write_text("print('v2')\n", encoding="utf-8")
        lines = self.evidence_lines(self.status_output())

        for key in ("verification", "stage06_evidence", "stage07_evidence", "stage08_evidence", "final_decision_status"):
            self.assertIn("changed paths: worktree:app.py", lines[key], key)
            self.assertNotIn(UNTRACKED_OUTPUT_HINT, lines[key], key)

        self.assertEqual(self.pipeline(), EXIT_SUCCESS)
        reasons = [
            json.loads(path.read_text(encoding="utf-8"))["reason"]
            for path in (orchestrator_dir(self.task_dir) / "history").glob("*/reason.json")
        ]
        self.assertTrue(any("worktree:app.py" in reason for reason in reasons), reasons)

    def test_r07_fr2_fr3_untracked_drift_lists_twenty_paths_and_hint(self):
        self.start()
        self.accept()
        (self.root / "gen").mkdir()
        for index in range(25):
            (self.root / "gen" / ("f%02d.py" % index)).write_text("", encoding="utf-8")

        lines = self.evidence_lines(self.status_output())

        for key in ("stage06_evidence", "stage07_evidence", "stage08_evidence"):
            self.assertIn("untracked:gen/f00.py", lines[key], key)
            self.assertIn("untracked:gen/f19.py", lines[key], key)
            self.assertNotIn("gen/f20.py", lines[key], key)
            self.assertIn("(and 5 more)", lines[key], key)
            self.assertIn(UNTRACKED_OUTPUT_HINT, lines[key], key)

    def test_r07_fr2_fr3_change_during_stage7_review_names_path(self):
        self.start()
        original = controller.ensure_real_stage

        def review_then_write(*args, **kwargs):
            code = original(*args, **kwargs)
            if args[3] == "07":
                self.pycache("review.cpython-3.pyc")
            return code

        patch(self, controller, "ensure_real_stage", review_then_write)

        self.assertEqual(self.pipeline(), EXIT_BLOCKED)

        reason = self.state()["last_failure"]["reason"]
        self.assertIn("during Stage 7 review", reason)
        self.assertIn("untracked:__pycache__/review.cpython-3.pyc", reason)
        self.assertIn(UNTRACKED_OUTPUT_HINT, reason)


# Inherited C06 tests already run from test_c06_evidence; collect R07 only.
for _name in list(vars(c06.C06EvidenceTests)):
    if _name.startswith("test_"):
        setattr(R07PipelineReasonTests, _name, None)
del _name


if __name__ == "__main__":
    unittest.main()
