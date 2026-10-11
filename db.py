from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator, Literal
from urllib.parse import parse_qs, urlparse

from config import settings

NoticeSaveStatus = Literal["new", "updated", "unchanged", "baseline"]
log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS summary_cache (
    cache_key TEXT PRIMARY KEY,
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_digest_reservations (
    user_id INTEGER NOT NULL,
    batch_id INTEGER NOT NULL,
    delivery_date TEXT NOT NULL,
    PRIMARY KEY(user_id,batch_id,delivery_date)
);
CREATE TABLE IF NOT EXISTS history_scan_state (
    id INTEGER PRIMARY KEY CHECK(id=1),
    next_page INTEGER NOT NULL DEFAULT 2,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scrape_run_pages (
    run_id INTEGER NOT NULL,
    phase TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    item_count INTEGER NOT NULL,
    stop_reason TEXT NOT NULL,
    PRIMARY KEY(run_id,phase,page_number)
);
CREATE TABLE IF NOT EXISTS scrape_failures (
    detail_url TEXT PRIMARY KEY,
    snapshot_json TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending',
    error_type TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scrape_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    pages INTEGER NOT NULL DEFAULT 0,
    new_count INTEGER NOT NULL DEFAULT 0,
    updated_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    error_type TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    scrape_succeeded INTEGER NOT NULL DEFAULT 0,
    delivered_count INTEGER NOT NULL DEFAULT 0,
    error_type TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS admin_alerts (
    alert_key TEXT PRIMARY KEY,
    attempted_at TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 1,
    error_type TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS delivery_resolutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS digest_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_key TEXT NOT NULL UNIQUE,
    message_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS batch_notices (
    batch_id INTEGER NOT NULL,
    notice_id INTEGER NOT NULL,
    revision_count INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    PRIMARY KEY (batch_id, notice_id)
);
CREATE TABLE IF NOT EXISTS batch_recipients (
    batch_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    email TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    smtp_code INTEGER,
    last_error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, user_id)
);
CREATE TABLE IF NOT EXISTS user_notice_deliveries (
    user_id INTEGER NOT NULL,
    notice_id INTEGER NOT NULL,
    revision_count INTEGER NOT NULL,
    batch_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    PRIMARY KEY (user_id, notice_id, revision_count)
);
CREATE TABLE IF NOT EXISTS history_authorizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    email_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'unsubscribed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    subscribed_at TEXT NOT NULL,
    consent_source TEXT NOT NULL,
    start_after_notice_id INTEGER NOT NULL DEFAULT 0
);

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
    notice_versions_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sending', 'sent', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
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
        delivery_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(mail_deliveries)")
        }
        _add_column_if_missing(
            conn,
            "mail_deliveries",
            delivery_columns,
            "failure_count",
            "INTEGER NOT NULL DEFAULT 0",
        )
        _add_column_if_missing(
            conn,
            "mail_deliveries",
            delivery_columns,
            "notice_versions_json",
            "TEXT NOT NULL DEFAULT '[]'",
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(notices)")}
        _add_column_if_missing(conn, "notices", columns, "fetched_date", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "notices", columns, "content_hash", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "notices", columns, "updated_at", "TEXT")
        _add_column_if_missing(
            conn, "notices", columns, "revision_count", "INTEGER NOT NULL DEFAULT 1"
        )
        conn.execute(
            "UPDATE notices SET fetched_date = substr(fetched_at, 1, 10) WHERE fetched_date = ''"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_notices_unsent ON notices(sent, id)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mail_deliveries_status "
            "ON mail_deliveries(status, updated_at)"
        )


def validate_email(email: str) -> str:
    """单个 ASCII 邮箱；保留 local-part 大小写，不合并别名。"""
    if not isinstance(email, str) or any(ord(char) < 32 or ord(char) > 126 for char in email):
        raise ValueError("邮箱必须是单个合法 ASCII 地址")
    email = email.strip()
    if len(email) > 254 or email.count("@") != 1:
        raise ValueError("邮箱格式无效")
    local, domain = email.split("@")
    if (
        not local
        or len(local) > 64
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", local)
        or "." not in domain
        or len(domain) > 253
        or any(
            not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in domain.split(".")
        )
    ):
        raise ValueError("邮箱格式无效")
    return f"{local}@{domain.lower()}"


def add_user(email: str, *, consent_source: str) -> int:
    address = validate_email(email)
    if not consent_source.strip():
        raise ValueError("必须记录订阅同意来源")
    now = datetime.now(settings.timezone).isoformat(timespec="seconds")
    with connect() as conn:
        existing = conn.execute("SELECT id FROM users WHERE email_key = ?", (address,)).fetchone()
        if existing:
            return int(existing["id"])
        start_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM notices").fetchone()[0]
        cursor = conn.execute(
            "INSERT INTO users (email, email_key, created_at, updated_at, subscribed_at, consent_source, start_after_notice_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (address, address, now, now, now, consent_source.strip(), start_id),
        )
        return int(cursor.lastrowid)


def get_users() -> list[dict]:
    with connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM users ORDER BY id")]


def get_active_users() -> list[dict]:
    with connect() as conn:
        return [
            dict(row)
            for row in conn.execute("SELECT * FROM users WHERE status = 'active' ORDER BY id")
        ]


def authorize_history(user_id: int, *, reason: str) -> None:
    """显式授权历史补发，不重置已接受、明确拒收及未知投递事实。"""
    if not reason.strip():
        raise ValueError("历史补发必须记录明确授权原因")
    now = datetime.now(settings.timezone).isoformat(timespec="seconds")
    with connect() as conn:
        user = conn.execute("SELECT status FROM users WHERE id=?", (user_id,)).fetchone()
        if not user or user["status"] != "active":
            raise ValueError("历史补发仅允许启用的用户")
        conn.execute(
            "INSERT INTO history_authorizations (user_id,reason,created_at) VALUES (?,?,?)",
            (user_id, reason.strip(), now),
        )
        conn.execute(
            "UPDATE users SET start_after_notice_id=0,updated_at=? WHERE id=?", (now, user_id)
        )
        conn.execute(
            "UPDATE batch_recipients SET status='pending',updated_at=? WHERE user_id=? AND status='skipped'",
            (now, user_id),
        )
        conn.execute(
            "UPDATE user_notice_deliveries SET status='pending' WHERE user_id=? AND status='skipped'",
            (user_id,),
        )


def set_user_status(user_id: int, status: str, *, consent_source: str = "") -> None:
    if status not in {"active", "paused", "unsubscribed"}:
        raise ValueError("订阅状态无效")
    now = datetime.now(settings.timezone).isoformat(timespec="seconds")
    with connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if user is None:
            raise ValueError("用户 ID 不存在")
        if status == "active" and user["status"] == "unsubscribed":
            if not consent_source.strip():
                raise ValueError("重新订阅需要新的同意来源")
            start_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM notices").fetchone()[0]
            conn.execute(
                "UPDATE users SET subscribed_at = ?, consent_source = ?, start_after_notice_id = ? WHERE id = ?",
                (now, consent_source.strip(), start_id, user_id),
            )
        conn.execute(
            "UPDATE users SET status = ?, updated_at = ? WHERE id = ?", (status, now, user_id)
        )


def get_notice_hash(detail_url: str) -> str | None:
    """不存在返回 None；v1.0 老数据尚未建立基线时返回空字符串。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT content_hash FROM notices WHERE detail_url = ? LIMIT 1",
            (detail_url,),
        ).fetchone()
    return None if row is None else str(row["content_hash"])


def get_history_scan_cursor() -> int:
    with connect() as conn:
        row = conn.execute("SELECT next_page FROM history_scan_state WHERE id=1").fetchone()
    return max(2, int(row[0])) if row else 2


def record_scan_page(
    run_id: int,
    phase: str,
    page_number: int,
    item_count: int,
    stop_reason: str,
    *,
    next_page: int | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO scrape_run_pages VALUES (?,?,?,?,?)",
            (run_id, phase, page_number, item_count, stop_reason),
        )
        if next_page is not None:
            conn.execute(
                "INSERT INTO history_scan_state (id,next_page,updated_at) VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET next_page=excluded.next_page,updated_at=excluded.updated_at",
                (max(2, next_page), datetime.now(settings.timezone).isoformat(timespec="seconds")),
            )


def get_notice_by_url(detail_url: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM notices WHERE detail_url=?", (detail_url,)).fetchone()
    return _row_to_notice(row) if row else None


def update_notice_hash_baseline(
    detail_url: str, old_hash: str, new_hash: str, attachments: list[dict]
) -> None:
    """哈希算法升级不制造新修订、不重置投递事实。调用者已核对内容一致。"""
    with connect() as conn:
        conn.execute(
            "UPDATE notices SET content_hash=?,attachments_json=? WHERE detail_url=? AND content_hash=?",
            (new_hash, json.dumps(attachments, ensure_ascii=False), detail_url, old_hash),
        )


def record_scrape_failure(notice: dict, error_type: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO scrape_failures (detail_url,snapshot_json,error_type,updated_at) VALUES (?,?,?,?) ON CONFLICT(detail_url) DO UPDATE SET snapshot_json=excluded.snapshot_json,attempts=CASE WHEN scrape_failures.status='resolved' THEN 1 ELSE scrape_failures.attempts+1 END,status=CASE WHEN scrape_failures.status='resolved' THEN 'pending' WHEN scrape_failures.attempts+1 >= 5 THEN 'exhausted' ELSE 'pending' END,error_type=excluded.error_type,updated_at=excluded.updated_at",
            (
                notice["detail_url"],
                json.dumps(notice, ensure_ascii=False),
                error_type,
                datetime.now(settings.timezone).isoformat(timespec="seconds"),
            ),
        )


def get_pending_scrape_failures() -> list[dict]:
    with connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM scrape_failures WHERE status='pending' AND attempts<5 ORDER BY updated_at,detail_url LIMIT 20"
            )
        ]


def resolve_scrape_failure(detail_url: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE scrape_failures SET status='resolved',updated_at=? WHERE detail_url=?",
            (datetime.now(settings.timezone).isoformat(timespec="seconds"), detail_url),
        )


def start_scrape_run() -> int:
    now = datetime.now(settings.timezone).isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute(
            "UPDATE scrape_runs SET status='interrupted',finished_at=?,error_type='InterruptedTask' WHERE status='running'",
            (now,),
        )
        return conn.execute("INSERT INTO scrape_runs (started_at) VALUES (?)", (now,)).lastrowid


def finish_scrape_run(run_id: int, stats, *, error_type: str = "") -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE scrape_runs SET finished_at=?,status=?,pages=?,new_count=?,updated_count=?,failed_count=?,error_type=? WHERE id=?",
            (
                datetime.now(settings.timezone).isoformat(timespec="seconds"),
                "failed" if error_type else "partial" if stats.failed else "success",
                stats.pages,
                stats.new,
                stats.updated,
                stats.failed,
                error_type,
                run_id,
            ),
        )


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
        rows = conn.execute("SELECT * FROM notices WHERE sent = 0 ORDER BY id").fetchall()
    return [_row_to_notice(row) for row in rows]


def ensure_delivery(
    message_key: str,
    message_id: str,
    notice_ids: list[int],
    notice_versions: list[dict] | None = None,
) -> dict:
    now_text = datetime.now(settings.timezone).isoformat(timespec="seconds")
    notice_ids_json = json.dumps(sorted(notice_ids))
    with connect() as conn:
        if notice_versions is None:
            placeholders = ",".join("?" for _ in notice_ids)
            notice_versions = (
                [
                    dict(row)
                    for row in conn.execute(
                        "SELECT id, revision_count, content_hash FROM notices "
                        f"WHERE id IN ({placeholders}) ORDER BY id",
                        notice_ids,
                    )
                ]
                if notice_ids
                else []
            )
        notice_versions_json = json.dumps(notice_versions, sort_keys=True)
        conn.execute(
            """
            INSERT OR IGNORE INTO mail_deliveries
                (message_key, message_id, notice_ids_json, notice_versions_json,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (message_key, message_id, notice_ids_json, notice_versions_json, now_text, now_text),
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
            SET status = 'failed', failure_count = failure_count + 1,
                last_error = ?, updated_at = ?
            WHERE message_key = ?
            """,
            (error[:4000], now_text, message_key),
        )


def complete_delivery(message_key: str, notice_ids: list[int]) -> None:
    """原子完成投递记录，仅确认与发送快照相符的通知修订。"""
    if not notice_ids:
        return
    now_text = datetime.now(settings.timezone).isoformat(timespec="seconds")
    with connect() as conn:
        delivery = conn.execute(
            "SELECT notice_versions_json FROM mail_deliveries WHERE message_key = ?",
            (message_key,),
        ).fetchone()
        if delivery is None:
            raise RuntimeError("邮件投递记录不存在")
        versions = json.loads(delivery["notice_versions_json"])
        if not versions:
            log.warning("投递记录缺少修订快照，未确认任何通知：%s", message_key)
        allowed_ids = set(notice_ids)
        conn.executemany(
            "UPDATE notices SET sent = 1, sent_at = ? "
            "WHERE id = ? AND revision_count = ? AND content_hash = ? AND sent = 0",
            [
                (now_text, version["id"], version["revision_count"], version["content_hash"])
                for version in versions
                if version["id"] in allowed_ids
            ],
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


def claim_admin_alert(alert_key: str, cooldown_seconds: int) -> bool:
    now = datetime.now(settings.timezone)
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT attempted_at FROM admin_alerts WHERE alert_key=?", (alert_key,)
        ).fetchone()
        if row and (now - datetime.fromisoformat(row[0])).total_seconds() < cooldown_seconds:
            return False
        conn.execute(
            "INSERT INTO admin_alerts (alert_key,attempted_at,status) VALUES (?,?,'sending') "
            "ON CONFLICT(alert_key) DO UPDATE SET attempted_at=excluded.attempted_at,status='sending',attempts=admin_alerts.attempts+1,error_type=''",
            (alert_key, now.isoformat(timespec="seconds")),
        )
        return True


def finish_admin_alert(alert_key: str, error_type: str = "") -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE admin_alerts SET status=?,error_type=? WHERE alert_key=?",
            ("failed" if error_type else "sent", error_type, alert_key),
        )


def start_task_run() -> int:
    """调用者持有任务锁；旧 running 表示进程中断，不伪造正常结束。"""
    now = datetime.now(settings.timezone).isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE task_runs SET status='interrupted',finished_at=?,error_type='InterruptedTask' WHERE status='running'",
            (now,),
        )
        return conn.execute("INSERT INTO task_runs (started_at) VALUES (?)", (now,)).lastrowid


def finish_task_run(
    run_id: int,
    *,
    status: str,
    scrape_succeeded: bool,
    delivered_count: int = 0,
    error_type: str = "",
) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE task_runs SET finished_at=?,status=?,scrape_succeeded=?,delivered_count=?,error_type=? WHERE id=?",
            (
                datetime.now(settings.timezone).isoformat(timespec="seconds"),
                status,
                int(scrape_succeeded),
                delivered_count,
                error_type,
                run_id,
            ),
        )


def get_task_health() -> dict:
    with connect() as conn:
        latest = conn.execute("SELECT * FROM task_runs ORDER BY id DESC LIMIT 1").fetchone()
        successful = conn.execute(
            "SELECT finished_at FROM task_runs WHERE scrape_succeeded=1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return {
        "latest": dict(latest) if latest else None,
        "last_successful_scrape_at": successful[0] if successful else None,
    }


def prepare_recipient_batches(users: list[dict]) -> None:
    """逐用户/修订建立稳定快照；不依赖旧版全局 sent。"""
    now = datetime.now(settings.timezone).isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        notices = [_row_to_notice(row) for row in conn.execute("SELECT * FROM notices ORDER BY id")]
        groups: dict[tuple, tuple[list[dict], list[dict]]] = {}
        for user in users:
            pending = [
                notice
                for notice in notices
                if notice["id"] > user["start_after_notice_id"]
                and not conn.execute(
                    "SELECT 1 FROM user_notice_deliveries WHERE user_id=? AND notice_id=? AND revision_count=?",
                    (user["id"], notice["id"], notice["revision_count"]),
                ).fetchone()
            ][: getattr(settings, "max_notices_per_digest", 20)]
            if not pending:
                continue
            fingerprint = tuple((n["id"], n["revision_count"], n["content_hash"]) for n in pending)
            groups.setdefault(fingerprint, (pending, []))[1].append(user)
        for fingerprint, (pending, recipients) in groups.items():
            # 最多 20 个 BCC 地址；收件人与通知版本共同决定批次身份。
            for offset in range(0, len(recipients), 20):
                subset = recipients[offset : offset + 20]
                key = hashlib.sha256(
                    json.dumps([fingerprint, [u["id"] for u in subset]], sort_keys=True).encode()
                ).hexdigest()
                cursor = conn.execute(
                    "INSERT INTO digest_batches (message_key,message_id,created_at,updated_at) VALUES (?,?,?,?)",
                    (key, f"<chd-batch-{key}@chd-notice.local>", now, now),
                )
                batch_id = cursor.lastrowid
                for notice in pending:
                    conn.execute(
                        "INSERT INTO batch_notices VALUES (?,?,?,?,?)",
                        (
                            batch_id,
                            notice["id"],
                            notice["revision_count"],
                            notice["content_hash"],
                            json.dumps(notice, ensure_ascii=False),
                        ),
                    )
                for user in subset:
                    conn.execute(
                        "INSERT INTO batch_recipients (batch_id,user_id,email,updated_at) VALUES (?,?,?,?)",
                        (batch_id, user["id"], user["email"], now),
                    )
                    for notice in pending:
                        conn.execute(
                            "INSERT INTO user_notice_deliveries (user_id,notice_id,revision_count,batch_id) VALUES (?,?,?,?)",
                            (user["id"], notice["id"], notice["revision_count"], batch_id),
                        )


def get_retryable_batches() -> list[dict]:
    with connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT DISTINCT b.* FROM digest_batches b JOIN batch_recipients r ON r.batch_id=b.id "
                "WHERE r.status IN ('pending','refused') AND r.attempts < 3 ORDER BY b.id"
            )
        ]


def get_batch_notices(batch_id: int) -> list[dict]:
    with connect() as conn:
        return [
            json.loads(row[0])
            for row in conn.execute(
                "SELECT snapshot_json FROM batch_notices WHERE batch_id=? ORDER BY notice_id",
                (batch_id,),
            )
        ]


def store_batch_summaries(batch_id: int, notices: list[dict]) -> None:
    with connect() as conn:
        conn.executemany(
            "UPDATE batch_notices SET snapshot_json=? WHERE batch_id=? AND notice_id=?",
            [(json.dumps(n, ensure_ascii=False), batch_id, n["id"]) for n in notices],
        )


def claim_batch_recipients(batch_id: int) -> list[dict]:
    """发送前复核订阅并领取；崩溃后的 sending 不自动再次发送。"""
    now = datetime.now(settings.timezone).isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT * FROM batch_recipients WHERE batch_id=? AND status IN ('pending','refused') AND attempts < 3 ORDER BY user_id",
            (batch_id,),
        ).fetchall()
        recipients = []
        minimum_id = conn.execute(
            "SELECT MIN(notice_id) FROM batch_notices WHERE batch_id=?", (batch_id,)
        ).fetchone()[0]
        for row in rows:
            user = conn.execute("SELECT * FROM users WHERE id=?", (row["user_id"],)).fetchone()
            eligible = (
                user
                and user["status"] == "active"
                and user["email"] == row["email"]
                and user["start_after_notice_id"] < minimum_id
            )
            if eligible:
                today = now[:10]
                # 同一固定快照的有限重试不占用第二封预算；未知结果也保守占用预算。
                reserved = conn.execute(
                    "SELECT 1 FROM daily_digest_reservations WHERE user_id=? AND batch_id=? AND delivery_date=?",
                    (row["user_id"], batch_id, today),
                ).fetchone()
                used = conn.execute(
                    "SELECT COUNT(*) FROM daily_digest_reservations WHERE user_id=? AND delivery_date=?",
                    (row["user_id"], today),
                ).fetchone()[0]
                # 升级前的今日发送事实也计入，避免部署当天重复投递。
                legacy = conn.execute(
                    "SELECT COUNT(*) FROM batch_recipients r WHERE r.user_id=? "
                    "AND r.status IN ('accepted','unknown','sending') AND substr(r.updated_at,1,10)=? "
                    "AND r.batch_id!=? AND NOT EXISTS (SELECT 1 FROM daily_digest_reservations d "
                    "WHERE d.user_id=r.user_id AND d.batch_id=r.batch_id AND d.delivery_date=?)",
                    (row["user_id"], today, batch_id, today),
                ).fetchone()[0]
                if not reserved and used + legacy >= getattr(
                    settings, "max_digest_messages_per_day", 1
                ):
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO daily_digest_reservations VALUES (?,?,?)",
                    (row["user_id"], batch_id, today),
                )
                recipients.append(dict(row))
            status = "sending" if eligible else "skipped"
            conn.execute(
                "UPDATE batch_recipients SET status=?,attempts=attempts+?,updated_at=? WHERE batch_id=? AND user_id=?",
                (status, int(bool(eligible)), now, batch_id, row["user_id"]),
            )
            conn.execute(
                "UPDATE user_notice_deliveries SET status=? WHERE batch_id=? AND user_id=?",
                (status, batch_id, row["user_id"]),
            )
        if rows:
            states = {
                r[0]
                for r in conn.execute(
                    "SELECT status FROM batch_recipients WHERE batch_id=?", (batch_id,)
                )
            }
            status = (
                "sending"
                if "sending" in states
                else "skipped"
                if states == {"skipped"}
                else "partial"
            )
            conn.execute(
                "UPDATE digest_batches SET status=?,updated_at=? WHERE id=?",
                (status, now, batch_id),
            )
        return recipients


def recover_interrupted_batches() -> int:
    """调用者必须持有任务锁。进程中断无法证明 SMTP 没有接受邮件。"""
    now = datetime.now(settings.timezone).isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT batch_id,user_id FROM batch_recipients WHERE status='sending'"
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE batch_recipients SET status='unknown',last_error='InterruptedTask',updated_at=? WHERE batch_id=? AND user_id=?",
                (now, row["batch_id"], row["user_id"]),
            )
            conn.execute(
                "UPDATE user_notice_deliveries SET status='unknown' WHERE batch_id=? AND user_id=?",
                (row["batch_id"], row["user_id"]),
            )
            conn.execute(
                "UPDATE digest_batches SET status='unknown',last_error='InterruptedTask',updated_at=? WHERE id=?",
                (now, row["batch_id"]),
            )
        return len(rows)


def get_unknown_recipients() -> list[dict]:
    with connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT batch_id,user_id,attempts,last_error FROM batch_recipients WHERE status='unknown' ORDER BY batch_id,user_id"
            )
        ]


def resolve_unknown_delivery(batch_id: int, user_id: int, *, decision: str, reason: str) -> None:
    """管理员核对后显式确认或授权一次有限重试，保留尝试次数和审计。"""
    if decision not in {"confirm", "retry"} or not reason.strip():
        raise ValueError("必须指定 confirm/retry 和核对原因")
    now = datetime.now(settings.timezone).isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM batch_recipients WHERE batch_id=? AND user_id=?", (batch_id, user_id)
        ).fetchone()
        if row is None or row["status"] != "unknown":
            raise ValueError("只能处理投递结果未知的收件人")
        if decision == "retry" and row["attempts"] >= 3:
            raise ValueError("已达到三次尝试上限，不能自动补发")
        conn.execute(
            "INSERT INTO delivery_resolutions (batch_id,user_id,decision,reason,created_at) VALUES (?,?,?,?,?)",
            (batch_id, user_id, decision, reason.strip(), now),
        )
        if decision == "confirm":
            # 使用同一个连接事务，复用修订确认逻辑，不能确认之后产生的新修订。
            _record_recipient_result(
                conn,
                batch_id,
                [dict(row)],
                {"accepted": [row["email"]], "refused": {}},
                error="AdministratorConfirmed",
            )
        else:
            conn.execute(
                "UPDATE batch_recipients SET status='pending',last_error='AdministratorRetry',updated_at=? WHERE batch_id=? AND user_id=?",
                (now, batch_id, user_id),
            )
            conn.execute(
                "UPDATE user_notice_deliveries SET status='pending' WHERE batch_id=? AND user_id=?",
                (batch_id, user_id),
            )
            conn.execute(
                "UPDATE digest_batches SET status=CASE WHEN EXISTS (SELECT 1 FROM batch_recipients WHERE batch_id=digest_batches.id AND status='unknown') THEN 'unknown' ELSE 'pending' END,updated_at=? WHERE id=?",
                (now, batch_id),
            )


def record_recipient_result(
    batch_id: int, recipients: list[dict], result: dict | None, *, error: str = ""
) -> None:
    with connect() as conn:
        _record_recipient_result(conn, batch_id, recipients, result, error=error)


def _record_recipient_result(conn, batch_id, recipients, result, *, error=""):
    now = datetime.now(settings.timezone).isoformat(timespec="seconds")
    for recipient in recipients:
        address = recipient["email"]
        code = None
        if result is None:
            status = "unknown"
        elif address in result["accepted"]:
            status = "accepted"
        else:
            code = int(result["refused"][address][0])
            status = "refused" if 400 <= code < 500 else "permanent"
        # 不保存 SMTP 原始回复，避免回复中包含同学邮箱或敏感信息。
        conn.execute(
            "UPDATE batch_recipients SET status=?,smtp_code=?,last_error=?,updated_at=? WHERE batch_id=? AND user_id=?",
            (
                status,
                code,
                error or (f"SMTP {code}" if code else ""),
                now,
                batch_id,
                recipient["user_id"],
            ),
        )
        conn.execute(
            "UPDATE user_notice_deliveries SET status=? WHERE batch_id=? AND user_id=?",
            (status, batch_id, recipient["user_id"]),
        )
    states = [
        row[0]
        for row in conn.execute("SELECT status FROM batch_recipients WHERE batch_id=?", (batch_id,))
    ]
    batch_status = (
        "unknown"
        if "unknown" in states
        else (
            "sent"
            if all(s in {"accepted", "skipped"} for s in states)
            else "partial"
            if "accepted" in states
            else "failed"
        )
    )
    conn.execute(
        "UPDATE digest_batches SET status=?,updated_at=?,last_error=? WHERE id=?",
        (batch_status, now, error, batch_id),
    )
    # sent 仅兼容旧查看方式；多人选择永远用逐用户修订状态。
    for snapshot in conn.execute(
        "SELECT * FROM batch_notices WHERE batch_id=?", (batch_id,)
    ).fetchall():
        outstanding = conn.execute(
            "SELECT 1 FROM users u WHERE u.status='active' AND u.start_after_notice_id < ? AND NOT EXISTS (SELECT 1 FROM user_notice_deliveries d WHERE d.user_id=u.id AND d.notice_id=? AND d.revision_count=? AND d.status='accepted') LIMIT 1",
            (snapshot["notice_id"], snapshot["notice_id"], snapshot["revision_count"]),
        ).fetchone()
        if not outstanding:
            conn.execute(
                "UPDATE notices SET sent=1,sent_at=? WHERE id=? AND revision_count=? AND content_hash=?",
                (
                    now,
                    snapshot["notice_id"],
                    snapshot["revision_count"],
                    snapshot["content_hash"],
                ),
            )
