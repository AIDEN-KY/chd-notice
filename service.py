from __future__ import annotations

import asyncio
import hashlib
import json
import logging

from config import settings
from db import (
    claim_admin_alert,
    claim_batch_recipients,
    finish_admin_alert,
    finish_task_run,
    get_active_users,
    get_batch_notices,
    get_retryable_batches,
    init_db,
    prepare_recipient_batches,
    record_recipient_result,
    recover_interrupted_batches,
    start_task_run,
    store_batch_summaries,
)
from mailer import send_digest, send_system_alert
from scraper import SessionExpiredError, scrape_notices
from summarizer import summarize
from task_lock import task_lock

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
    alert_key = hashlib.sha256(title.encode("utf-8")).hexdigest()
    try:
        if not claim_admin_alert(alert_key, getattr(settings, "alert_cooldown_seconds", 3600)):
            log.info("相同管理员故障告警处于冷却期，本次不重复发送")
            return
    except Exception as exc:
        log.error("无法保存告警限频状态，未发送告警；错误类型=%s", type(exc).__name__)
        return
    error_type = ""
    try:
        await asyncio.to_thread(send_system_alert, title, detail[:6000])
        log.info("系统故障告警已发送：%s", title)
    except Exception as exc:
        error_type = type(exc).__name__
        log.error("系统故障告警发送失败；错误类型=%s", error_type)
    try:
        finish_admin_alert(alert_key, error_type)
    except Exception as exc:
        log.error("无法保存告警结果；错误类型=%s", type(exc).__name__)


async def send_pending_today() -> int:
    if not getattr(settings, "multi_user_delivery_ready", False):
        return await _send_pending_today()
    with task_lock(settings.database_file.with_suffix(".task.lock")):
        init_db()
        recovered = recover_interrupted_batches()
        if recovered:
            log.warning("发现 %d 个中断投递，已标记 unknown，需管理员核对", recovered)
        return await _send_pending_today()


async def _send_pending_today() -> int:
    """发送全部历史待发通知，不再次触发抓取。命令名为兼容 v1.0 保留。"""
    init_db()
    users = get_active_users()
    if not users:
        log.info("没有启用的订阅者，不生成摘要、不发送日报；通知保持待发")
        return 0
    if not getattr(settings, "multi_user_delivery_ready", False):
        log.warning("多人投递尚未完成收件人状态及并发保护，正式日报暂不发送")
        return 0
    prepare_recipient_batches(users)
    delivered = 0
    for batch in get_retryable_batches():
        notices = get_batch_notices(batch["id"])
        for notice in notices:
            if "summary" not in notice:
                notice["summary"] = await summarize(notice["title"], notice["content_html"])
        store_batch_summaries(batch["id"], notices)
        recipients = claim_batch_recipients(batch["id"])
        if not recipients:
            continue
        addresses = [recipient["email"] for recipient in recipients]
        envelope_identity = hashlib.sha256(
            json.dumps([(r["user_id"], r["attempts"] + 1) for r in recipients]).encode()
        ).hexdigest()[:16]
        message_id = batch["message_id"].replace("@", f"-{envelope_identity}@")
        try:
            result = await asyncio.to_thread(
                send_digest, notices, batch["created_at"][:10], message_id, recipients=addresses
            )
            if not isinstance(result, dict):
                raise ValueError("SMTP transport must return explicit recipient results")
            accepted = result.get("accepted")
            refused = result.get("refused")
            if (
                not isinstance(accepted, list)
                or not isinstance(refused, dict)
                or set(accepted) & set(refused)
                or set(accepted) | set(refused) != set(addresses)
                or len(accepted) != len(set(accepted))
            ):
                raise ValueError("SMTP result does not match the actual envelope")
            for rejection in refused.values():
                if (
                    not isinstance(rejection, (tuple, list))
                    or len(rejection) != 2
                    or not 400 <= int(rejection[0]) <= 599
                ):
                    raise ValueError("Invalid SMTP refusal result")
        except Exception as exc:
            record_recipient_result(batch["id"], recipients, None, error=type(exc).__name__)
            log.error(
                "批次 %s 投递结果未知，停止自动重试；错误类型=%s", batch["id"], type(exc).__name__
            )
            raise
        record_recipient_result(batch["id"], recipients, result)
        if result["refused"]:
            log.warning(
                "批次 %s：SMTP 接受 %d 人，拒收 %d 人；仅明确临时拒收者可有限补发",
                batch["id"],
                len(result["accepted"]),
                len(result["refused"]),
            )
        if result["accepted"]:
            delivered += len(notices)
    return delivered


async def run_daily() -> int:
    with task_lock(settings.database_file.with_suffix(".task.lock")):
        return await _run_daily()


async def _run_daily() -> int:
    try:
        init_db()
        recovered = recover_interrupted_batches()
        if recovered:
            log.warning("发现 %d 个中断投递，需管理员核对", recovered)
    except Exception as exc:
        await _try_send_alert("数据库初始化失败", type(exc).__name__)
        raise

    run_id = start_task_run()
    scrape_succeeded = False
    error_type = ""
    try:
        stats = await scrape_notices(headless=True)
        scrape_succeeded = True
        log.info(
            "本次抓取完成：新增 %d，更新 %d，未变化 %d，建立基线 %d，共 %d 页",
            stats.new,
            stats.updated,
            stats.unchanged,
            stats.baselined,
            stats.pages,
        )
    except SessionExpiredError as exc:
        error_type = type(exc).__name__
        log.error("CAS 会话已过期；跳过抓取，继续处理数据库中的待发送通知")
        await _try_send_alert(
            "CAS 会话已过期",
            "请在项目目录运行：python main.py init-auth",
        )
    except Exception as exc:
        error_type = type(exc).__name__
        log.error("抓取失败；继续处理库存；错误类型=%s", error_type)
        await _try_send_alert("通知抓取失败", error_type)

    try:
        delivered_count = await _send_pending_today()
    except Exception as exc:
        finish_task_run(
            run_id,
            status="failed",
            scrape_succeeded=scrape_succeeded,
            error_type=type(exc).__name__,
        )
        await _try_send_alert("日报发送任务失败", type(exc).__name__)
        raise
    finish_task_run(
        run_id,
        status="success" if scrape_succeeded else "degraded",
        scrape_succeeded=scrape_succeeded,
        delivered_count=delivered_count,
        error_type=error_type,
    )
    return 0 if scrape_succeeded else 3
