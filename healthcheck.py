from __future__ import annotations

import logging
import os
from pathlib import Path

from config import settings
from db import get_task_health

log = logging.getLogger(__name__)


def run_healthcheck() -> bool:
    """只检查本地运行条件，不联网、不抓取、不发送邮件。"""
    ok = True
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
        try:
            directory.mkdir(parents=True, exist_ok=True)
            log.info("目录可用：%s", directory)
        except OSError as exc:
            log.error("目录不可用：%s (%s)", directory, exc)
            ok = False

    if settings.database_file.exists():
        log.info("数据库存在：%s", settings.database_file)
        try:
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
    else:
        log.info("数据库将在首次运行时创建：%s", settings.database_file)

    log.info("配置检查%s", "通过" if ok else "未通过")
    if not settings.multi_user_delivery_ready:
        log.warning("多人正式发送保护尚未完成，日报发送入口保持关闭；管理员测试命令不群发")
    return ok
