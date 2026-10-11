from __future__ import annotations

import logging
import os
from pathlib import Path

from config import settings
from db import get_task_health, validate_email

log = logging.getLogger(__name__)


def run_healthcheck() -> bool:
    """只检查本地运行条件，不联网、不抓取、不发送邮件。"""
    ok = True
    try:
        if hasattr(settings, "unsubscribe_url"):
            settings.unsubscribe_url
    except ValueError:
        log.error("UNSUBSCRIBE_URL 配置无效（不显示配置值）")
        ok = False
    required = ("QQ_EMAIL", "QQ_SMTP_AUTH_CODE", "MY_EMAIL")
    try:
        if settings.ai_summary_enabled:
            required += ("DEEPSEEK_API_KEY",)
            settings.ai_excluded_keywords
        else:
            log.info("AI 摘要已关闭，无需 DeepSeek API Key")
    except ValueError as exc:
        log.error("AI 配置无效：%s", exc)
        ok = False
    for name in required:
        if os.getenv(name, "").strip():
            log.info("配置已填写：%s", name)
        else:
            log.error("缺少环境变量：%s", name)
            ok = False
        if any(character in os.getenv(name, "") for character in ("\r", "\n")):
            log.error("%s 不得包含换行符（不显示配置值）", name)
            ok = False
    try:
        if hasattr(settings, "history_scan_enabled"):
            settings.history_scan_enabled
    except ValueError:
        log.error("HISTORY_SCAN_ENABLED 配置无效（不显示配置值）")
        ok = False
    for name in ("QQ_EMAIL", "MY_EMAIL"):
        if os.getenv(name, "").strip():
            try:
                validate_email(os.getenv(name, ""))
            except ValueError:
                log.error("%s 邮箱格式无效（不显示配置值）", name)
                ok = False

    if settings.auth_file.is_file():
        log.info("登录会话文件存在：%s", settings.auth_file)
    else:
        log.error("缺少 auth.json，请运行 main.py init-auth")
        ok = False

    browser_path = os.getenv("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if browser_path and Path(browser_path).is_dir():
        log.info("Playwright 浏览器目录有效：%s", browser_path)
    elif browser_path:
        log.error("PLAYWRIGHT_BROWSERS_PATH 指向的目录不存在：%s", browser_path)
        ok = False
    else:
        log.warning("未设置 PLAYWRIGHT_BROWSERS_PATH，将使用 Playwright 默认目录")

    for directory in (settings.attachment_dir, settings.log_dir):
        if directory.exists() and not directory.is_dir():
            log.error("运行目录路径不是文件夹")
            ok = False
        elif not directory.exists():
            log.info("运行目录尚未创建；doctor 不创建目录")

    if settings.database_file.exists():
        log.info("数据库存在：%s", settings.database_file)
        try:
            from backup import inspect_database

            version = inspect_database(settings.database_file)
            log.info("数据库 schema 版本=%d", version)
            health = get_task_health()
            if health["latest"]:
                log.info(
                    "最近每日任务：状态=%s 开始=%s 结束=%s；最近成功抓取=%s",
                    health["latest"]["status"],
                    health["latest"]["started_at"],
                    health["latest"]["finished_at"],
                    health["last_successful_scrape_at"],
                )
            else:
                log.info("尚无每日任务健康记录")
        except Exception as exc:
            log.warning("无法读取任务健康记录；错误类型=%s", type(exc).__name__)
            ok = False
    else:
        log.info("数据库将在首次运行时创建：%s", settings.database_file)

    log.info("配置检查%s", "通过" if ok else "未通过")
    if not settings.multi_user_delivery_ready:
        log.warning("多人正式发送保护尚未完成，日报发送入口保持关闭；管理员测试命令不群发")
    return ok
