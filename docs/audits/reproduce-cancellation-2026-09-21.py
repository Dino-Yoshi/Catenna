"""Show cancellation behavior with a harmless sleeping subprocess."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    stdout = root / "stdout"
    stderr = root / "stderr"
    child_code = "import os,time; print(os.getpid(), flush=True); time.sleep(30)"
    worker_code = "\n".join([
        "from pathlib import Path",
        "from agent_pipeline.real_runner import run_to_files",
        "from agent_pipeline.locking import TaskLock",
        "root = Path(" + repr(str(root)) + ")",
        "with TaskLock(root, 'audit', 'cancel-probe'):",
        "    run_to_files(" + repr([sys.executable, "-c", child_code]) + ", root/'stdout', root/'stderr', 35)",
    ])
    worker = subprocess.Popen([sys.executable, "-c", worker_code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    child_pid = None
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if stdout.exists() and stdout.read_text().strip():
                child_pid = int(stdout.read_text().strip())
                break
            time.sleep(0.02)
        assert child_pid is not None
        worker.send_signal(signal.SIGINT)
        worker.wait(timeout=5)
        child_live = True
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            child_live = False
        lock_exists = (root / ".orchestrator" / "lock.json").exists()
        print(json.dumps({"probe": "interrupt_releases_lock_but_leaves_child_alive", "controller_exit": worker.returncode, "child_alive": child_live, "lock_exists": lock_exists}))
        assert child_live and not lock_exists
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait()
        if child_pid is not None:
            try:
                os.killpg(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
