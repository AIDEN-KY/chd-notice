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
from db import (  # noqa: E402  # noqa: E402
    add_user,
    authorize_history,
    get_delivery_preview,
    get_unknown_recipients,
    get_users,
    init_db,
    recover_interrupted_batches,
    resolve_unknown_delivery,
    set_user_status,
)
from healthcheck import run_healthcheck  # noqa: E402
from scraper import (  # noqa: E402
    diagnose_list_api,
    diagnose_pagination,
    initialize_auth,
    scrape_notices,
)
from service import run_daily, send_pending_today  # noqa: E402
from task_lock import TaskAlreadyRunningError, task_lock  # noqa: E402
from version import VERSION  # noqa: E402


def configure_logging() -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
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
    def daily_job():
        try:
            exit_code = asyncio.run(run_daily())
            if exit_code:
                logging.getLogger(__name__).warning("每日任务降级完成，状态码=%d", exit_code)
        except TaskAlreadyRunningError as exc:
            logging.getLogger(__name__).warning("%s", exc)

    scheduler = BlockingScheduler(timezone=settings.timezone)
    scheduler.add_job(
        daily_job,
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
            "preview",
            "diagnose-api",
            "diagnose-pagination",
            "init-auth",
            "scrape",
            "send-now",
            "send-pending",
            "send-test",
            "scheduler",
            "users",
            "deliveries",
        ],
        help="配置检查 / 初始化登录 / 立即抓取 / 完整日报 / 仅发送全部待发通知 / 启动定时器",
    )
    parser.add_argument("--notice-id", type=int, help="send-test 指定数据库通知 ID")
    parser.add_argument(
        "--action",
        choices=[
            "list",
            "add",
            "pause",
            "resume",
            "unsubscribe",
            "import-self",
            "backfill",
            "confirm",
            "retry",
        ],
    )
    parser.add_argument("--email", help="users add 使用单个邮箱")
    parser.add_argument("--user-id", type=int, help="users 状态修改使用 ID")
    parser.add_argument("--consent-source", help="添加或重新订阅时记录同意来源")
    parser.add_argument("--batch-id", type=int, help="deliveries 核对批次 ID")
    parser.add_argument("--reason", help="deliveries 人工核对依据（不要填敏感信息）")
    args = parser.parse_args()
    if args.notice_id is not None and args.command != "send-test":
        parser.error("--notice-id 仅用于 send-test")
    if args.command not in {"users", "deliveries"} and any(
        value is not None for value in (args.action, args.email, args.user_id, args.consent_source)
    ):
        parser.error("用户管理参数仅用于 users")
    if args.command != "deliveries" and (args.batch_id is not None or args.reason is not None):
        parser.error("投递核对参数仅用于 deliveries")
    if args.command == "deliveries":
        if (
            args.action not in {None, "list", "confirm", "retry"}
            or args.email
            or args.consent_source
        ):
            parser.error("deliveries 只支持 list/confirm/retry")
        if args.action in {"confirm", "retry"} and (
            args.batch_id is None or args.user_id is None or not args.reason
        ):
            parser.error("核对必须指定 --batch-id、--user-id 和 --reason")
    if args.command == "users":
        if args.action in {"confirm", "retry"}:
            parser.error("users 不支持投递核对操作")
        if args.action is None:
            args.action = "list"
        if args.action in {"add", "import-self"} and not args.consent_source:
            parser.error("添加用户必须指定 --consent-source")
        if args.action == "add" and not args.email:
            parser.error("users add 必须指定 --email")
        if args.action in {"pause", "resume", "unsubscribe", "backfill"} and args.user_id is None:
            parser.error("修改用户状态必须指定 --user-id")
    if args.command == "preview":
        print(json.dumps(get_delivery_preview(), ensure_ascii=False, indent=2))
        return
    configure_logging()
    if args.command != "send-test":
        init_db()
    if args.command == "deliveries":
        with task_lock(settings.database_file.with_suffix(".task.lock")):
            recover_interrupted_batches()
            if args.action in {None, "list"}:
                for row in get_unknown_recipients():
                    print(
                        f"批次={row['batch_id']} 用户={row['user_id']} 尝试={row['attempts']} 错误={row['last_error']}"
                    )
            else:
                try:
                    resolve_unknown_delivery(
                        args.batch_id, args.user_id, decision=args.action, reason=args.reason
                    )
                except ValueError as exc:
                    parser.error(str(exc))
                print("核对记录已保存；本命令不发送邮件")
    elif args.command == "users":
        try:
            if args.action in {"add", "import-self"}:
                user_id = add_user(
                    settings.my_email if args.action == "import-self" else args.email,
                    consent_source=args.consent_source,
                )
                print(f"用户 ID：{user_id}；重复添加不会恢复停用状态")
            elif args.action == "list":
                for user in get_users():
                    print(
                        f"ID={user['id']} 状态={user['status']} 历史起点={user['start_after_notice_id']}"
                    )
            elif args.action == "backfill":
                authorize_history(args.user_id, reason=args.consent_source or "")
                print(f"用户 ID={args.user_id} 已授权历史补发；正式发送仍受发布保护")
            else:
                status = {"pause": "paused", "resume": "active", "unsubscribe": "unsubscribed"}[
                    args.action
                ]
                set_user_status(args.user_id, status, consent_source=args.consent_source or "")
                print(f"用户 ID={args.user_id} 状态={status}")
        except ValueError as exc:
            parser.error(str(exc))
    elif args.command == "doctor":
        raise SystemExit(0 if run_healthcheck() else 1)
    elif args.command == "init-auth":
        with task_lock(settings.database_file.with_suffix(".task.lock")):
            asyncio.run(initialize_auth())
    elif args.command == "diagnose-api":
        with task_lock(settings.database_file.with_suffix(".task.lock")):
            diagnostics = asyncio.run(diagnose_list_api())
        settings.api_diagnostics_file.write_text(
            json.dumps(diagnostics, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"已保存 {len(diagnostics)} 个接口摘要：{settings.api_diagnostics_file}")
    elif args.command == "diagnose-pagination":
        with task_lock(settings.database_file.with_suffix(".task.lock")):
            diagnostics = asyncio.run(diagnose_pagination(headless=False, wait_for_user=True))
        print(json.dumps(diagnostics, ensure_ascii=False, indent=2))
    elif args.command == "scrape":
        with task_lock(settings.database_file.with_suffix(".task.lock")):
            stats = asyncio.run(scrape_notices())
        print(
            f"抓取完成：新增 {stats.new} 条，更新 {stats.updated} 条，"
            f"未变化 {stats.unchanged} 条，建立基线 {stats.baselined} 条，失败 {stats.failed} 条"
        )
        if stats.failed:
            raise SystemExit(3)
    elif args.command == "send-now":
        raise SystemExit(asyncio.run(run_daily()))
    elif args.command == "send-pending":
        asyncio.run(send_pending_today())
    elif args.command == "send-test":
        from tests.manual_email.sender import send_test_email

        with task_lock(settings.database_file.with_suffix(".task.lock")):
            asyncio.run(send_test_email(args.notice_id))
    else:
        run_scheduler()


if __name__ == "__main__":
    try:
        main()
    except TaskAlreadyRunningError as exc:
        logging.getLogger(__name__).warning("%s", exc)
        raise SystemExit(2) from None
