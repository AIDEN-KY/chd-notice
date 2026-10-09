from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

from config import settings


SCHEMA = """
CREATE TABLE IF NOT EXISTS notices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    publish_time TEXT NOT NULL DEFAULT '',
    department TEXT NOT NULL DEFAULT '',
    detail_url TEXT NOT NULL UNIQUE,
    content_html TEXT NOT NULL DEFAULT '',
    attachments_json TEXT NOT NULL DEFAULT '[]',
    fetched_at TEXT NOT NULL,
    fetched_date TEXT NOT NULL,
    sent INTEGER NOT NULL DEFAULT 0 CHECK (sent IN (0, 1)),
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


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(notices)")}
        if "fetched_date" not in columns:
            conn.execute("ALTER TABLE notices ADD COLUMN fetched_date TEXT NOT NULL DEFAULT ''")
            conn.execute("UPDATE notices SET fetched_date = substr(fetched_at, 1, 10) WHERE fetched_date = ''")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_notices_daily_unsent "
            "ON notices(fetched_date, sent)"
        )


def upsert_notice(notice: dict) -> bool:
    now = datetime.now(settings.timezone)
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO notices
                (title, publish_time, department, detail_url, content_html,
                 attachments_json, fetched_at, fetched_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                notice["title"], notice.get("publish_time", ""),
                notice.get("department", ""), notice["detail_url"],
                notice.get("content_html", ""),
                json.dumps(notice.get("attachments", []), ensure_ascii=False),
                now.isoformat(timespec="seconds"), now.date().isoformat(),
            ),
        )
        return cursor.rowcount == 1


def get_unsent_for_date(date_text: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM notices WHERE fetched_date = ? AND sent = 0 ORDER BY id",
            (date_text,),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["attachments"] = json.loads(item.pop("attachments_json"))
        except json.JSONDecodeError:
            item["attachments"] = []
        result.append(item)
    return result


def mark_sent(ids: list[int], sent_at: datetime) -> None:
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    with connect() as conn:
        conn.execute(
            f"UPDATE notices SET sent = 1, sent_at = ? WHERE id IN ({placeholders})",
            [sent_at.isoformat(timespec="seconds"), *ids],
        )
