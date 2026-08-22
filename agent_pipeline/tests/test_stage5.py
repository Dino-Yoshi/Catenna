from __future__ import print_function

import tempfile
import unittest
from pathlib import Path

from agent_pipeline.artifacts import sha256_file
from agent_pipeline.mock_agent import valid_artifact
from agent_pipeline.stage5 import stage5_run_matches_report


def _base_run(**overrides):
    run = {
        "candidate_artifact_path": None,
        "run_id": "run-test",
        "pass_number": 1,
        "attempt_number": 1,
        "attempt_kind": "normal",
        "retry_reason": "initial/no-retry",
        "agent": "claude",
        "execution_mode": "workspace-write",
        "exit_code": 0,
        "failure_class": None,
        "metadata_path": None,
        "stdout_path": None,
        "stderr_path": None,
        "dirty_baseline": {"entries": [], "hashes": {}},
    }
    run.update(overrides)
    return run


class Stage5RunMatchesReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)

        self.report_body = valid_artifact("05")
        self.report_path = self.tmp_path / "05_codex_implementation_report.md"
        self.report_path.write_text(self.report_body, encoding="utf-8")
        self.report_hash = sha256_file(self.report_path)

        self.metadata_path = self.tmp_path / "run.json"
        self.metadata_path.write_text("{}", encoding="utf-8")
        self.stdout_path = self.tmp_path / "run.stdout"
        self.stdout_path.write_text("", encoding="utf-8")
        self.stderr_path = self.tmp_path / "run.stderr"
        self.stderr_path.write_text("", encoding="utf-8")

        self.state = {"dirty_baseline": {"entries": [], "hashes": {}}}

    def _finish_paths(self, run):
        run["metadata_path"] = str(self.metadata_path)
        run["stdout_path"] = str(self.stdout_path)
        run["stderr_path"] = str(self.stderr_path)
        return run

    def test_matches_when_final_artifact_hash_recorded_despite_stripped_preamble(self):
        # normalize_stage_output strips provider commentary before the required
        # heading when finalizing -- the raw candidate file legitimately differs
        # from the finalized report whenever that happened. final_artifact_hash
        # is computed from the normalized (finalized) content, not the raw
        # candidate, and must be trusted over a raw re-hash of the candidate.
        candidate_path = self.tmp_path / "candidate.md"
        candidate_path.write_text(
            "Here's the report, as requested.\n\n" + self.report_body, encoding="utf-8"
        )
        run = self._finish_paths(_base_run(
            candidate_artifact_path=str(candidate_path),
            final_artifact_hash=self.report_hash,
        ))

        self.assertTrue(stage5_run_matches_report(run, self.report_path, self.report_hash, self.state))

    def test_rejects_when_final_artifact_hash_does_not_match_report(self):
        candidate_path = self.tmp_path / "candidate.md"
        candidate_path.write_text(self.report_body, encoding="utf-8")
        run = self._finish_paths(_base_run(
            candidate_artifact_path=str(candidate_path),
            final_artifact_hash="not-the-real-hash",
        ))

        self.assertFalse(stage5_run_matches_report(run, self.report_path, self.report_hash, self.state))

    def test_falls_back_to_raw_candidate_hash_when_final_artifact_hash_absent(self):
        # Legacy runs recorded before final_artifact_hash existed: preserve the
        # original exact-match behavior against the raw candidate file.
        candidate_path = self.tmp_path / "candidate.md"
        candidate_path.write_text(self.report_body, encoding="utf-8")
        run = self._finish_paths(_base_run(candidate_artifact_path=str(candidate_path)))
        run.pop("final_artifact_hash", None)

        self.assertTrue(stage5_run_matches_report(run, self.report_path, self.report_hash, self.state))

    def test_legacy_fallback_still_rejects_mismatched_raw_candidate(self):
        candidate_path = self.tmp_path / "candidate.md"
        candidate_path.write_text("completely different content\n", encoding="utf-8")
        run = self._finish_paths(_base_run(candidate_artifact_path=str(candidate_path)))
        run.pop("final_artifact_hash", None)

        self.assertFalse(stage5_run_matches_report(run, self.report_path, self.report_hash, self.state))


if __name__ == "__main__":
    unittest.main()
