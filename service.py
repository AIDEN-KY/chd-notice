from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from config import settings
from db import get_unsent_for_date, init_db, mark_sent
from mailer import send_digest
from scraper import SessionExpiredError, scrape_notices
from summarizer import summarize

log = logging.getLogger(__name__)


async def send_pending_today() -> int:
    """发送今天已入库但尚未发送的通知，不再次触发抓取。"""
    init_db()
    today = datetime.now(settings.timezone).date().isoformat()
    notices = get_unsent_for_date(today)
    if not notices:
        log.info("今天没有新增且未发送的通知，不发送邮件")
        return 0
    for index, notice in enumerate(notices, start=1):
        log.info("正在生成摘要 %d/%d：%s", index, len(notices), notice["title"])
        notice["summary"] = await summarize(notice["title"], notice["content_html"])
    log.info("正在发送日报，共 %d 条通知", len(notices))
    # SMTP 是阻塞 I/O，放到线程中，避免阻塞异步任务。
    await asyncio.to_thread(send_digest, notices, today)
    mark_sent([item["id"] for item in notices], datetime.now(settings.timezone))
    log.info("日报发送成功，共 %d 条通知", len(notices))
    return len(notices)


async def run_daily() -> None:
    init_db()
    try:
        added = await scrape_notices(headless=True)
        log.info("本次抓取完成，新增 %d 条", added)
    except SessionExpiredError as exc:
        log.error("%s；跳过抓取，继续处理数据库中已有未发送通知", exc)
    except Exception:
        log.exception("抓取失败；跳过抓取，继续处理数据库中已有未发送通知")

    await send_pending_today()
