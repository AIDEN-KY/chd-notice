from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from contextlib import closing
from datetime import datetime

from config import settings
from db import validate_email
from mailer import send_digest
from summarizer import summarize

log = logging.getLogger(__name__)


async def send_bcc_test_email(*, confirm_send: bool = False) -> int:
    """显式试投：合成正文，两自有邮箱，正式群发保护不变。"""
    if not confirm_send:
        raise ValueError("需要 --confirm-send 确认向两个自有邮箱发送合成测试邮件")
    recipients = [validate_email(settings.my_email), validate_email(settings.qq_email)]
    if len(set(recipients)) != 2:
        raise ValueError("MY_EMAIL 与 QQ_EMAIL 必须为两个不同的自有测试邮箱")
    if settings.database_file.exists():
        uri = settings.database_file.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='users'").fetchone():
                for address in recipients:
                    row = conn.execute(
                        "SELECT status FROM users WHERE email_key=?", (address,)
                    ).fetchone()
                    if row and row[0] != "active":
                        raise ValueError("测试收件人已暂停或退订，不发送测试邮件")
    notice = {
        "title": "合成通知：双邮箱 BCC 验收",
        "content_html": "<p>本通知完全合成，不含学校通知。活动时间：2030年1月1日18:00；"
        "活动地点：测试报告厅；联系人：测试老师；邮箱：teacher@example.test。</p>",
        "detail_url": "https://example.com/",
        "attachments": [],
    }
    notice["summary"] = await summarize(notice["title"], notice["content_html"], use_cache=False)
    try:
        result = await asyncio.to_thread(
            send_digest,
            [notice],
            datetime.now(settings.timezone).date().isoformat(),
            None,
            subject_prefix="【合成 BCC 测试】",
            recipients=recipients,
        )
    except Exception as exc:
        raise RuntimeError(f"合成 BCC 测试未确认成功；错误类型={type(exc).__name__}") from None
    if result.get("refused") or set(result.get("accepted", [])) != set(recipients):
        raise ValueError("测试存在拒收或不完整投递结果；请核对收件，不自动重试")
    log.info("合成 BCC 测试：SMTP 接受 2 个地址；请人工确认收件，不修改正式投递记录")
    return 2


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
