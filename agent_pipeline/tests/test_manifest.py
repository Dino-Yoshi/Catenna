from __future__ import print_function

import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_pipeline.manifest import capture_dirty_baseline, changed_files_since, entry_path, git_status


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.git("init")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test User")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.check_output(["git"] + list(args), cwd=str(self.repo))

    def commit_file(self, path, text):
        full = self.repo / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text, encoding="utf-8")
        self.git("add", path)
        self.git("commit", "-m", "commit " + path)

    def test_git_status_z_returns_legacy_strings_for_literal_paths(self):
        path = 'space "quote" \u00e9.txt'
        self.commit_file(path, "base\n")
        (self.repo / path).write_text("changed\n", encoding="utf-8")

        entries = git_status(self.repo)

        self.assertEqual(entries, [" M " + path])
        self.assertEqual(entry_path(entries[0]), path)

    def test_git_status_z_normalizes_rename_to_current_path(self):
        self.commit_file("old name.txt", "base\n")
        self.git("mv", "old name.txt", "new name.txt")

        entries = git_status(self.repo)

        self.assertEqual(entries, ["R  old name.txt -> new name.txt"])
        self.assertEqual(entry_path(entries[0]), "new name.txt")

    def test_changed_files_reports_reverted_to_clean_with_baseline_hash(self):
        self.commit_file("tracked.txt", "base\n")
        (self.repo / "tracked.txt").write_text("dirty before\n", encoding="utf-8")
        baseline = capture_dirty_baseline(self.repo)
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")

        changed = changed_files_since(self.repo, baseline)

        self.assertEqual(changed, [{"path": "tracked.txt", "reason": "reverted_to_clean_during_stage5"}])

    def test_changed_files_reports_deleted_before_restored_clean_without_hash(self):
        self.commit_file("tracked.txt", "base\n")
        (self.repo / "tracked.txt").unlink()
        baseline = capture_dirty_baseline(self.repo)
        self.git("checkout", "HEAD", "--", "tracked.txt")

        changed = changed_files_since(self.repo, baseline)

        self.assertEqual(changed, [{"path": "tracked.txt", "reason": "reverted_to_clean_during_stage5"}])

    def test_changed_files_reports_new_untracked_file_as_new_since_dirty_baseline(self):
        baseline = capture_dirty_baseline(self.repo)
        (self.repo / "new_file.txt").write_text("created after baseline\n", encoding="utf-8")

        changed = changed_files_since(self.repo, baseline)

        self.assertEqual(changed, [{"path": "new_file.txt", "reason": "new_since_dirty_baseline"}])

    def test_changed_files_reports_missing_baseline_path_as_deleted(self):
        baseline = {"captured_at": "now", "entries": ["?? gone.txt"], "hashes": {}}

        changed = changed_files_since(self.repo, baseline)

        self.assertEqual(changed, [{"path": "gone.txt", "reason": "deleted_during_stage5"}])


if __name__ == "__main__":
    unittest.main()
