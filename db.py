from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator, Literal
from urllib.parse import parse_qs, urlparse

from config import settings

NoticeSaveStatus = Literal["new", "updated", "unchanged", "baseline"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS notices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    publish_time TEXT NOT NULL DEFAULT '',
    department TEXT NOT NULL DEFAULT '',
    detail_url TEXT NOT NULL UNIQUE,
    content_html TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    attachments_json TEXT NOT NULL DEFAULT '[]',
    fetched_at TEXT NOT NULL,
    fetched_date TEXT NOT NULL,
    updated_at TEXT,
    revision_count INTEGER NOT NULL DEFAULT 1,
    sent INTEGER NOT NULL DEFAULT 0 CHECK (sent IN (0, 1)),
    sent_at TEXT
);

CREATE TABLE IF NOT EXISTS mail_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_key TEXT NOT NULL UNIQUE,
    message_id TEXT NOT NULL,
    notice_ids_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sending', 'sent', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sent_at TEXT
);
"""


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(settings.database_file, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _add_column_if_missing(
    conn: sqlite3.Connection,
    table: str,
    columns: set[str],
    name: str,
    definition: str,
) -> None:
    if name not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def init_db() -> None:
    """创建数据库，并原地升级 v1.0 数据库。"""
    with connect() as conn:
        conn.executescript(SCHEMA)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(notices)")}
        _add_column_if_missing(conn, "notices", columns, "fetched_date", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "notices", columns, "content_hash", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "notices", columns, "updated_at", "TEXT")
        _add_column_if_missing(
            conn, "notices", columns, "revision_count", "INTEGER NOT NULL DEFAULT 1"
        )
        conn.execute(
            "UPDATE notices SET fetched_date = substr(fetched_at, 1, 10) "
            "WHERE fetched_date = ''"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_notices_unsent ON notices(sent, id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mail_deliveries_status "
            "ON mail_deliveries(status, updated_at)"
        )


def get_notice_hash(detail_url: str) -> str | None:
    """不存在返回 None；v1.0 老数据尚未建立基线时返回空字符串。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT content_hash FROM notices WHERE detail_url = ? LIMIT 1",
            (detail_url,),
        ).fetchone()
    return None if row is None else str(row["content_hash"])


def get_existing_notice_urls() -> dict[str, str]:
    """按门户稳定 ID 找回原 URL，兼容旧版截断的 lm 参数。"""
    with connect() as conn:
        rows = conn.execute("SELECT detail_url FROM notices ORDER BY id").fetchall()
    urls = {}
    for row in rows:
        fragment = urlparse(row["detail_url"]).fragment
        identifier = parse_qs(fragment.partition("?")[2]).get("dm", [""])[0]
        if identifier:
            urls.setdefault(identifier, row["detail_url"])
    return urls


def save_notice(notice: dict, content_hash: str) -> NoticeSaveStatus:
    """保存新通知或新版本；老数据首次建立哈希时不会被重新标记为待发送。"""
    now = datetime.now(settings.timezone)
    now_text = now.isoformat(timespec="seconds")
    attachments_json = json.dumps(notice.get("attachments", []), ensure_ascii=False)
    values = (
        notice["title"],
        notice.get("publish_time", ""),
        notice.get("department", ""),
        notice.get("content_html", ""),
        content_hash,
        attachments_json,
        notice["detail_url"],
    )

    with connect() as conn:
        existing = conn.execute(
            "SELECT id, content_hash FROM notices WHERE detail_url = ?",
            (notice["detail_url"],),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO notices
                    (title, publish_time, department, content_html, content_hash,
                     attachments_json, detail_url, fetched_at, fetched_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*values[:-1], values[-1], now_text, now.date().isoformat()),
            )
            return "new"

        old_hash = str(existing["content_hash"] or "")
        if old_hash == content_hash:
            return "unchanged"

        if not old_hash:
            conn.execute(
                """
                UPDATE notices
                SET title = ?, publish_time = ?, department = ?, content_html = ?,
                    content_hash = ?, attachments_json = ?
                WHERE detail_url = ?
                """,
                values,
            )
            return "baseline"

        conn.execute(
            """
            UPDATE notices
            SET title = ?, publish_time = ?, department = ?, content_html = ?,
                content_hash = ?, attachments_json = ?, updated_at = ?,
                revision_count = revision_count + 1, sent = 0, sent_at = NULL
            WHERE detail_url = ?
            """,
            (*values[:-1], now_text, values[-1]),
        )
        return "updated"


def _row_to_notice(row: sqlite3.Row) -> dict:
    item = dict(row)
    try:
        item["attachments"] = json.loads(item.pop("attachments_json"))
    except (json.JSONDecodeError, TypeError):
        item["attachments"] = []
    return item


def get_unsent_notices() -> list[dict]:
    """返回全部历史待发通知，避免跨天后永久滞留。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM notices WHERE sent = 0 ORDER BY id"
        ).fetchall()
    return [_row_to_notice(row) for row in rows]


def ensure_delivery(message_key: str, message_id: str, notice_ids: list[int]) -> dict:
    now_text = datetime.now(settings.timezone).isoformat(timespec="seconds")
    notice_ids_json = json.dumps(sorted(notice_ids))
    with connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO mail_deliveries
                (message_key, message_id, notice_ids_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (message_key, message_id, notice_ids_json, now_text, now_text),
        )
        row = conn.execute(
            "SELECT * FROM mail_deliveries WHERE message_key = ?",
            (message_key,),
        ).fetchone()
    if row is None:  # pragma: no cover
        raise RuntimeError("无法创建邮件投递记录")
    return dict(row)


def start_delivery(message_key: str) -> int:
    now_text = datetime.now(settings.timezone).isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute(
            """
            UPDATE mail_deliveries
            SET status = 'sending', attempts = attempts + 1,
                last_error = '', updated_at = ?
            WHERE message_key = ?
            """,
            (now_text, message_key),
        )
        row = conn.execute(
            "SELECT attempts FROM mail_deliveries WHERE message_key = ?",
            (message_key,),
        ).fetchone()
    if row is None:
        raise RuntimeError("邮件投递记录不存在")
    return int(row["attempts"])


def fail_delivery(message_key: str, error: str) -> None:
    now_text = datetime.now(settings.timezone).isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute(
            """
            UPDATE mail_deliveries
            SET status = 'failed', last_error = ?, updated_at = ?
            WHERE message_key = ?
            """,
            (error[:4000], now_text, message_key),
        )


def complete_delivery(message_key: str, notice_ids: list[int]) -> None:
    """在一个 SQLite 事务中完成投递记录和通知状态更新。"""
    if not notice_ids:
        return
    now_text = datetime.now(settings.timezone).isoformat(timespec="seconds")
    placeholders = ",".join("?" for _ in notice_ids)
    with connect() as conn:
        conn.execute(
            f"UPDATE notices SET sent = 1, sent_at = ? WHERE id IN ({placeholders})",
            [now_text, *notice_ids],
        )
        conn.execute(
            """
            UPDATE mail_deliveries
            SET status = 'sent', last_error = '', updated_at = ?, sent_at = ?
            WHERE message_key = ?
            """,
            (now_text, now_text, message_key),
        )


def get_delivery(message_key: str) -> dict | None:
    """供诊断和测试读取投递状态。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM mail_deliveries WHERE message_key = ?",
            (message_key,),
        ).fetchone()
    return None if row is None else dict(row)
