"""SQLite 在线快照和离线恢复演练；只发布到新的本地文件。"""

import os
import re
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path

from db import SCHEMA, SCHEMA_VERSION


def inspect_database(path: Path) -> int:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("数据库完整性检查失败")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if not 0 <= version <= SCHEMA_VERSION:
            raise ValueError("数据库 schema 版本不兼容")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(notices)")}
        if not {"id", "title", "detail_url", "content_html"} <= columns:
            raise ValueError("不是可识别的通知数据库")
        if version == SCHEMA_VERSION:
            required = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA))
            actual = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if not required <= actual:
                raise ValueError("当前版本数据库缺少必要表")
        return version


def copy_database(source: Path, destination: Path) -> dict:
    """在线备份 API 复制全部表；原子拒绝覆盖，保留投递 unknown 等事实。"""
    if Path(destination).is_symlink():
        raise ValueError("目标不能是符号链接")
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if any(str(path).startswith(("\\\\", "//")) for path in (source, destination)):
        raise ValueError("仅支持本地备份和恢复文件")
    if source == destination or destination.exists() or destination.is_symlink():
        raise ValueError("目标必须是新的文件，禁止覆盖")
    if not source.is_file() or not destination.parent.is_dir():
        raise ValueError("源文件或目标父目录不存在")
    inspect_database(source)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".chd-backup-", suffix=".db", dir=destination.parent
    )
    os.close(descriptor)
    temporary_path = Path(temporary)
    deadline = time.monotonic() + 60

    def progress(_status, _remaining, _total):
        if time.monotonic() >= deadline:
            raise TimeoutError("备份超过时间预算")

    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as reader:
            with closing(sqlite3.connect(temporary_path)) as writer:
                reader.backup(writer, pages=256, progress=progress, sleep=0.01)
        version = inspect_database(temporary_path)
        with temporary_path.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        # link 的目标存在时必定失败，避免检查之后有人创建目标所导致的覆盖。
        os.link(temporary_path, destination)
        return {"status": "ok", "schema_version": version}
    finally:
        temporary_path.unlink(missing_ok=True)
