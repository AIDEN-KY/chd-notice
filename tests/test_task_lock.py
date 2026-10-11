"""跨进程锁契约：离线子进程，不访问门户或 SMTP。"""

import importlib
import subprocess
import sys


def test_task_lock_excludes_an_independent_process(tmp_path):
    locks = importlib.import_module("task_lock")
    path = tmp_path / "daily.lock"
    script = """
import sys
from task_lock import task_lock, TaskAlreadyRunningError
try:
    with task_lock(sys.argv[1]):
        print('unexpected-acquisition')
except TaskAlreadyRunningError:
    print('locked')
"""
    with locks.task_lock(path):
        result = subprocess.run(
            [sys.executable, "-c", script, str(path)],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        assert result.stdout.strip() == "locked"
    with locks.task_lock(path):
        assert path.exists()


def test_task_lock_releases_after_process_crash_and_ignores_stale_file(tmp_path):
    locks = importlib.import_module("task_lock")
    path = tmp_path / "daily.lock"
    path.touch()
    script = """
import os, sys
from task_lock import task_lock
with task_lock(sys.argv[1]):
    print('acquired', flush=True)
    os._exit(7)
"""
    result = subprocess.run(
        [sys.executable, "-u", "-c", script, str(path)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 7
    assert result.stdout.strip() == "acquired"
    with locks.task_lock(path):
        assert path.exists()
