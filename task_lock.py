"""单机 OS 文件锁；锁文件始终保留，不能删除正在使用的锁文件。"""

import errno
import os
from contextlib import contextmanager
from pathlib import Path


class TaskAlreadyRunningError(RuntimeError):
    """另一个进程正在执行任务。"""


@contextmanager
def task_lock(path):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        # Windows 字节锁需要一个实际字节；并发追加不影响第零字节的锁。
        if path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise TaskAlreadyRunningError("已有任务正在运行，本次未执行") from exc
            raise
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
