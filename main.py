from __future__ import annotations

import argparse
import asyncio
import json
import logging
from logging.handlers import TimedRotatingFileHandler

from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

load_dotenv()

from config import settings  # noqa: E402
from db import init_db  # noqa: E402
from healthcheck import run_healthcheck  # noqa: E402
from scraper import (  # noqa: E402
    diagnose_list_api,
    diagnose_pagination,
    initialize_auth,
    scrape_notices,
)
from service import run_daily, send_pending_today  # noqa: E402
from version import VERSION  # noqa: E402


def configure_logging() -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    file_handler = TimedRotatingFileHandler(
        settings.log_dir / "chd-notice.log",
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logging.basicConfig(
        level=logging.INFO,
        handlers=[console, file_handler],
        force=True,
    )


def run_scheduler() -> None:
    scheduler = BlockingScheduler(timezone=settings.timezone)
    scheduler.add_job(
        lambda: asyncio.run(run_daily()),
        trigger="cron",
        hour=settings.schedule_hour,
        minute=settings.schedule_minute,
        id="daily_notice_digest",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    logging.getLogger(__name__).info("调度器已启动，每天 21:00 执行")
    scheduler.start()


def main() -> None:
    parser = argparse.ArgumentParser(description="长安大学通知监控与每日邮件推送系统")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument(
        "command",
        choices=[
            "doctor",
            "diagnose-api",
            "diagnose-pagination",
            "init-auth",
            "scrape",
            "send-now",
            "send-pending",
            "scheduler",
        ],
        help="配置检查 / 初始化登录 / 立即抓取 / 完整日报 / 仅发送全部待发通知 / 启动定时器",
    )
    args = parser.parse_args()
    configure_logging()
    init_db()
    if args.command == "doctor":
        raise SystemExit(0 if run_healthcheck() else 1)
    if args.command == "init-auth":
        asyncio.run(initialize_auth())
    elif args.command == "diagnose-api":
        diagnostics = asyncio.run(diagnose_list_api())
        settings.api_diagnostics_file.write_text(
            json.dumps(diagnostics, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(
            f"已保存 {len(diagnostics)} 个接口摘要："
            f"{settings.api_diagnostics_file}"
        )
    elif args.command == "diagnose-pagination":
        diagnostics = asyncio.run(
            diagnose_pagination(headless=False, wait_for_user=True)
        )
        print(json.dumps(diagnostics, ensure_ascii=False, indent=2))
    elif args.command == "scrape":
        stats = asyncio.run(scrape_notices())
        print(
            f"抓取完成：新增 {stats.new} 条，更新 {stats.updated} 条，"
            f"未变化 {stats.unchanged} 条，建立基线 {stats.baselined} 条"
        )
    elif args.command == "send-now":
        asyncio.run(run_daily())
    elif args.command == "send-pending":
        asyncio.run(send_pending_today())
    else:
        run_scheduler()


if __name__ == "__main__":
    main()
