from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime

from config import settings
from db import validate_email
from mailer import send_digest
from summarizer import summarize

log = logging.getLogger(__name__)


def get_test_notice(notice_id: int | None = None) -> dict | None:
    # URI 只读模式，不创建数据库、不迁移表结构。
    uri = settings.database_file.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        if notice_id is not None:
            rows = conn.execute("SELECT * FROM notices WHERE id = ?", (notice_id,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM notices ORDER BY id DESC").fetchall()
    notices = []
    for row in rows:
        item = dict(row)
        item["attachments"] = json.loads(item.pop("attachments_json") or "[]")
        notices.append(item)
    return next((item for item in notices if item["attachments"]), notices[0] if notices else None)


async def send_test_email(notice_id: int | None = None) -> int:
    # 旧库没有 users 时仅允许原管理员联调；新库中的停用状态不得绕过。
    with sqlite3.connect(settings.database_file.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        has_users = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'users'"
        ).fetchone()
        if has_users:
            user = conn.execute(
                "SELECT status FROM users WHERE email_key = ?", (validate_email(settings.my_email),)
            ).fetchone()
            if user and user[0] != "active":
                raise ValueError("测试收件人已暂停或退订，不发送测试邮件")
    notice = get_test_notice(notice_id)
    if notice is None:
        raise ValueError("未找到可测试通知，请先抓取或检查 --notice-id")
    log.info("发送测试邮件：通知 ID=%s，%s；不修改正式投递状态", notice["id"], notice["title"])
    notice["summary"] = await summarize(notice["title"], notice["content_html"])
    date_text = datetime.now(settings.timezone).date().isoformat()
    await asyncio.to_thread(send_digest, [notice], date_text, None, subject_prefix="【测试邮件】")
    log.info("测试邮件发送成功；重复运行会再次发送测试邮件")
    return 1
