from __future__ import annotations

import logging
import os
from pathlib import Path

from config import settings

log = logging.getLogger(__name__)


def run_healthcheck() -> bool:
    """只检查本地运行条件，不联网、不抓取、不发送邮件。"""
    ok = True
    required = ("DEEPSEEK_API_KEY", "QQ_EMAIL", "QQ_SMTP_AUTH_CODE", "MY_EMAIL")
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
    else:
        log.info("数据库将在首次运行时创建：%s", settings.database_file)

    log.info("配置检查%s", "通过" if ok else "未通过")
    return ok
