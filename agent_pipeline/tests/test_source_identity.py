from __future__ import print_function

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_pipeline import source_identity
from agent_pipeline.source_identity import (
    SourceIdentityError,
    capture_source_identity,
    format_identity,
    identity_problem,
    parse_cited_identity,
    same_identity,
)


def git(root, *args):
    subprocess.check_call(
        ["git", "-c", "user.name=Catenna Test", "-c", "user.email=test@example.invalid"] + list(args),
        cwd=str(root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class SourceIdentityFingerprintTests(unittest.TestCase):
    """C06-FR1: every relevant kind of source change moves the fingerprint."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        git(self.root, "init", "-q")
        (self.root / "app.py").write_text("print('v1')\n", encoding="utf-8")
        (self.root / "lib.py").write_text("X = 1\n", encoding="utf-8")
        (self.root / ".gitignore").write_text("build/\n*.log\n", encoding="utf-8")
        git(self.root, "add", ".")
        git(self.root, "commit", "-q", "-m", "base")

    def fp(self, excludes=None):
        return capture_source_identity(self.root, excludes)["fingerprint"]

    def assertChanges(self, mutate):
        before = self.fp()
        mutate()
        self.assertNotEqual(before, self.fp())

    def assertUnchanged(self, mutate, excludes=None):
        before = self.fp(excludes)
        mutate()
        self.assertEqual(before, self.fp(excludes))

    def test_unchanged_source_is_stable_and_self_consistent(self):
        first = capture_source_identity(self.root)
        second = capture_source_identity(self.root)
        self.assertIsNone(identity_problem(first))
        self.assertTrue(same_identity(first, second))
        self.assertRegex(first["head"], r"^[0-9a-f]{40}$")

    def test_committed_change(self):
        def commit():
            (self.root / "lib.py").write_text("X = 2\n", encoding="utf-8")
            git(self.root, "commit", "-q", "-am", "change")
        self.assertChanges(commit)

    def test_commit_with_identical_tree_still_changes_base(self):
        self.assertChanges(lambda: git(self.root, "commit", "-q", "--allow-empty", "-m", "empty"))

    def test_staged_change_with_worktree_restored(self):
        def stage_only():
            (self.root / "lib.py").write_text("X = 3\n", encoding="utf-8")
            git(self.root, "add", "lib.py")
            (self.root / "lib.py").write_text("X = 1\n", encoding="utf-8")
        self.assertChanges(stage_only)

    def test_dirty_edit_and_second_edit_of_already_dirty_file(self):
        self.assertChanges(lambda: (self.root / "app.py").write_text("print('dirty')\n", encoding="utf-8"))
        # Porcelain status stays " M app.py"; the content fingerprint must move.
        self.assertChanges(lambda: (self.root / "app.py").write_text("print('dirtier')\n", encoding="utf-8"))

    def test_deletion(self):
        self.assertChanges(lambda: (self.root / "lib.py").unlink())

    def test_staged_deletion(self):
        self.assertChanges(lambda: git(self.root, "rm", "-q", "lib.py"))

    def test_rename(self):
        self.assertChanges(lambda: git(self.root, "mv", "lib.py", "renamed.py"))

    def test_untracked_non_ignored_file(self):
        self.assertChanges(lambda: (self.root / "new_module.py").write_text("raise RuntimeError\n", encoding="utf-8"))

    def test_untracked_file_content_change(self):
        (self.root / "new_module.py").write_text("a\n", encoding="utf-8")
        self.assertChanges(lambda: (self.root / "new_module.py").write_text("b\n", encoding="utf-8"))

    def test_mode_change(self):
        self.assertChanges(lambda: os.chmod(str(self.root / "app.py"), 0o755))

    def test_ignored_build_output_is_outside_identity(self):
        def build():
            (self.root / "build").mkdir()
            (self.root / "build" / "out.bin").write_bytes(b"\x00\x01")
            (self.root / "run.log").write_text("log\n", encoding="utf-8")
        self.assertUnchanged(build)

    def test_controller_owned_data_is_excluded(self):
        def write_controller_data():
            task = self.root / ".agent-pipeline" / "tasks" / "t"
            task.mkdir(parents=True)
            (task / "06_manual_test_notes.md").write_text("notes\n", encoding="utf-8")
            (self.root / "external-tasks").mkdir()
            (self.root / "external-tasks" / "state.json").write_text("{}\n", encoding="utf-8")
        self.assertUnchanged(write_controller_data, excludes=[self.root / "external-tasks"])

    def test_excluding_the_worktree_root_is_ignored(self):
        before = self.fp(excludes=[self.root])
        (self.root / "new.py").write_text("x\n", encoding="utf-8")
        self.assertNotEqual(before, self.fp(excludes=[self.root]))

    def test_tracked_symlink_target_is_represented_without_following(self):
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("secret v1\n", encoding="utf-8")
        os.symlink(str(outside), str(self.root / "link"))
        git(self.root, "add", "link")
        git(self.root, "commit", "-q", "-m", "link")
        # Content outside the worktree is not read through the link.
        self.assertUnchanged(lambda: outside.write_text("secret v2\n", encoding="utf-8"))

        def retarget():
            (self.root / "link").unlink()
            os.symlink("app.py", str(self.root / "link"))
        self.assertChanges(retarget)

    def test_path_behind_symlinked_directory_is_not_read(self):
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "mod.py").write_text("A = 1\n", encoding="utf-8")
        git(self.root, "add", "pkg")
        git(self.root, "commit", "-q", "-m", "pkg")
        outside = Path(self.tmp.name) / "outside_pkg"
        outside.mkdir()
        (outside / "mod.py").write_text("A = 1\n", encoding="utf-8")
        opened = []
        original_open = source_identity.os.open

        def tracking_open(path, *args, **kwargs):
            opened.append(str(path))
            return original_open(path, *args, **kwargs)

        import shutil
        shutil.rmtree(str(self.root / "pkg"))
        os.symlink(str(outside), str(self.root / "pkg"))
        source_identity.os.open = tracking_open
        try:
            identity = capture_source_identity(self.root)
        finally:
            source_identity.os.open = original_open
        self.assertIsNone(identity_problem(identity))
        self.assertFalse(any(path.endswith("pkg/mod.py") for path in opened))

    def test_unborn_repository_is_a_valid_identity(self):
        fresh = Path(self.tmp.name) / "fresh"
        fresh.mkdir()
        git(fresh, "init", "-q")
        (fresh / "a.py").write_text("x\n", encoding="utf-8")
        identity = capture_source_identity(fresh)
        self.assertIsNone(identity["head"])
        self.assertIsNone(identity_problem(identity))


class SourceIdentityIntegrityTests(unittest.TestCase):
    """C06-FR5: incomplete, unreadable, or inconsistent snapshots stay unverified."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        git(self.root, "init", "-q")
        (self.root / "app.py").write_text("x\n", encoding="utf-8")

    def test_not_a_git_worktree_raises(self):
        plain = self.root / "plain"
        plain.mkdir()
        env_ceiling = os.environ.get("GIT_CEILING_DIRECTORIES")
        os.environ["GIT_CEILING_DIRECTORIES"] = str(self.root)
        try:
            with self.assertRaises(SourceIdentityError):
                capture_source_identity(plain)
        finally:
            if env_ceiling is None:
                os.environ.pop("GIT_CEILING_DIRECTORIES", None)
            else:
                os.environ["GIT_CEILING_DIRECTORIES"] = env_ceiling

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can read mode-000 files")
    def test_unreadable_file_raises(self):
        secret = self.root / "secret.py"
        secret.write_text("x\n", encoding="utf-8")
        os.chmod(str(secret), 0)
        self.addCleanup(os.chmod, str(secret), 0o644)
        with self.assertRaises(SourceIdentityError):
            capture_source_identity(self.root)

    def test_change_during_capture_raises(self):
        original = source_identity._hash_entries
        calls = []

        def racing(top, paths, allow_missing):
            if not calls:
                (self.root / "raced.py").write_text("y\n", encoding="utf-8")
                subprocess.check_call(["git", "add", "raced.py"], cwd=str(self.root))
            calls.append(1)
            return original(top, paths, allow_missing)

        source_identity._hash_entries = racing
        try:
            with self.assertRaises(SourceIdentityError):
                capture_source_identity(self.root)
        finally:
            source_identity._hash_entries = original

    def test_untracked_file_vanishing_during_capture_raises(self):
        (self.root / "temp.py").write_text("y\n", encoding="utf-8")
        original = source_identity._entry

        def vanish(top, rel, allow_missing):
            if rel == "temp.py" and (top / rel).exists():
                (top / rel).unlink()
            return original(top, rel, allow_missing)

        source_identity._entry = vanish
        try:
            with self.assertRaises(SourceIdentityError):
                capture_source_identity(self.root)
        finally:
            source_identity._entry = original

    def test_incomplete_or_tampered_records_are_rejected(self):
        identity = capture_source_identity(self.root)
        self.assertIsNone(identity_problem(identity))
        for mutate in (
            lambda d: d.pop("worktree"),
            lambda d: d.update(fingerprint="0" * 64),
            lambda d: d.update(untracked="not-a-hash"),
            lambda d: d.update(schema=99),
            lambda d: d.update(head="HEAD"),
        ):
            broken = dict(identity)
            mutate(broken)
            self.assertIsNotNone(identity_problem(broken))
            self.assertFalse(same_identity(broken, identity))
        self.assertIsNotNone(identity_problem(None))
        self.assertFalse(same_identity(None, None))


class CitationParsingTests(unittest.TestCase):
    def test_parses_exactly_the_cited_identity_section(self):
        fp = "a" * 64
        text = "# Stage 6 - Manual test notes\n\n## Source identity\n\n%s\n\n## Decision\n\n- [x] Accept\n" % ("sha256:" + fp)
        self.assertEqual(parse_cited_identity(text), ([fp], 1))

    def test_identity_outside_the_section_is_not_a_citation(self):
        text = "# Notes\n\nTested %s\n\n## Decision\n\n- [x] Accept\n" % ("b" * 64)
        self.assertEqual(parse_cited_identity(text), ([], 0))

    def test_multiple_distinct_citations_are_reported(self):
        text = "## Source identity\n\n%s\n%s\n" % ("a" * 64, "sha256:" + "b" * 64)
        cited, sections = parse_cited_identity(text)
        self.assertEqual(sections, 1)
        self.assertEqual(len(cited), 2)

    def test_format_round_trip(self):
        identity = {"fingerprint": "c" * 64}
        self.assertEqual(parse_cited_identity("## Source identity\n" + format_identity(identity))[0], ["c" * 64])


if __name__ == "__main__":
    unittest.main()
