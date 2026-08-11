from __future__ import print_function

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class PackagingSmokeTests(unittest.TestCase):
    def test_non_editable_install_runs_mock_test(self):
        if os.environ.get("CATENNA_PACKAGING_SMOKE") != "1":
            self.skipTest("set CATENNA_PACKAGING_SMOKE=1 to run packaging smoke test")

        repo_root = Path(__file__).resolve().parents[2]
        if not (repo_root / "pyproject.toml").exists():
            self.skipTest("packaging smoke test requires a source checkout")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wheel_dir = tmp_path / "wheelhouse"
            wheel_dir.mkdir()
            venv_dir = tmp_path / "venv"

            subprocess.check_call(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--no-deps",
                    "--no-build-isolation",
                    "--wheel-dir",
                    str(wheel_dir),
                    str(repo_root),
                ]
            )
            wheel = next(wheel_dir.glob("agent_pipeline-*.whl"))

            subprocess.check_call([sys.executable, "-m", "venv", str(venv_dir)])
            python = venv_dir / "bin" / "python"
            catenna = venv_dir / "bin" / "catenna"
            subprocess.check_call([str(python), "-m", "pip", "install", "--no-index", str(wheel)])
            subprocess.check_call([str(catenna), "mock-test"], cwd=str(tmp_path))
