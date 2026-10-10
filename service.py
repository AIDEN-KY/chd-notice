from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime

from config import settings
from db import (
    complete_delivery,
    ensure_delivery,
    fail_delivery,
    get_unsent_notices,
    init_db,
    start_delivery,
)
from mailer import send_digest, send_system_alert
from scraper import SessionExpiredError, scrape_notices
from summarizer import summarize

log = logging.getLogger(__name__)


def delivery_identity(notices: list[dict]) -> tuple[str, str]:
    """通知 ID、修订号和内容哈希共同决定一次投递的稳定身份。"""
    payload = [
        {
            "id": int(item["id"]),
            "revision": int(item.get("revision_count") or 1),
            "content_hash": str(item.get("content_hash") or ""),
        }
        for item in sorted(notices, key=lambda value: int(value["id"]))
    ]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    message_key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return message_key, f"<chd-notice-{message_key[:32]}@chd-notice.local>"


async def _try_send_alert(title: str, detail: str) -> None:
    try:
        await asyncio.to_thread(send_system_alert, title, detail[:6000])
        log.info("系统故障告警已发送：%s", title)
    except Exception:
        log.exception("系统故障告警发送失败：%s", title)


async def send_pending_today() -> int:
    """发送全部历史待发通知，不再次触发抓取。命令名为兼容 v1.0 保留。"""
    init_db()
    run_date = datetime.now(settings.timezone).date().isoformat()
    notices = get_unsent_notices()
    if not notices:
        log.info("数据库中没有待发送通知，不发送邮件")
        return 0

    notice_ids = [int(item["id"]) for item in notices]
    notice_versions = [
        {
            "id": int(item["id"]),
            "revision_count": int(item["revision_count"]),
            "content_hash": str(item["content_hash"] or ""),
        }
        for item in notices
    ]
    message_key, message_id = delivery_identity(notices)
    delivery = ensure_delivery(message_key, message_id, notice_ids, notice_versions)
    if delivery["status"] == "sent":
        complete_delivery(message_key, notice_ids)
        log.warning("投递记录已成功，已按修订快照核对通知状态而不重复投递")
        return 0

    for index, notice in enumerate(notices, start=1):
        log.info("正在生成摘要 %d/%d：%s", index, len(notices), notice["title"])
        notice["summary"] = await summarize(notice["title"], notice["content_html"])

    attempt = start_delivery(message_key)
    log.info("正在发送日报，共 %d 条通知（第 %d 次尝试）", len(notices), attempt)
    try:
        await asyncio.to_thread(send_digest, notices, run_date, message_id)
    except Exception as exc:
        fail_delivery(message_key, f"{type(exc).__name__}: {exc}")
        log.exception("日报发送失败，投递状态已记录")
        raise

    complete_delivery(message_key, notice_ids)
    log.info("日报发送成功，共 %d 条通知", len(notices))
    return len(notices)


async def run_daily() -> None:
    try:
        init_db()
    except Exception as exc:
        await _try_send_alert("数据库初始化失败", f"{type(exc).__name__}: {exc}")
        raise

    try:
        stats = await scrape_notices(headless=True)
        log.info(
            "本次抓取完成：新增 %d，更新 %d，未变化 %d，建立基线 %d，共 %d 页",
            stats.new,
            stats.updated,
            stats.unchanged,
            stats.baselined,
            stats.pages,
        )
    except SessionExpiredError as exc:
        log.error("%s；跳过抓取，继续处理数据库中的待发送通知", exc)
        await _try_send_alert(
            "CAS 会话已过期",
            f"{exc}\n\n请在项目目录运行：python main.py init-auth",
        )
    except Exception as exc:
        log.exception("抓取失败；跳过抓取，继续处理数据库中的待发送通知")
        await _try_send_alert("通知抓取失败", f"{type(exc).__name__}: {exc}")

    try:
        await send_pending_today()
    except Exception as exc:
        await _try_send_alert("日报发送任务失败", f"{type(exc).__name__}: {exc}")
        raise
