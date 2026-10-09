from __future__ import annotations

from types import SimpleNamespace
from typing import Coroutine, TypeVar
from zoneinfo import ZoneInfo

import pytest

import db
import service
from scraper import ScrapeStats, SessionExpiredError

T = TypeVar("T")


def _run_immediate(coroutine: Coroutine[object, object, T]) -> T:
    """运行不会真正挂起的测试协程，避免测试环境创建网络型事件循环。"""
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    raise AssertionError("测试协程意外进入挂起状态")


def _configure_temp_database(tmp_path, monkeypatch) -> None:
    test_settings = SimpleNamespace(
        database_file=tmp_path / "notices.db",
        timezone=ZoneInfo("Asia/Shanghai"),
    )
    monkeypatch.setattr(db, "settings", test_settings)
    monkeypatch.setattr(service, "settings", test_settings)
    db.init_db()


def _insert_historical_notice() -> None:
    db.save_notice(
        {
            "title": "历史待发通知",
            "publish_time": "2026-10-01",
            "department": "测试部门",
            "detail_url": "https://example.test/old-notice",
            "content_html": "<p>测试正文</p>",
            "attachments": [],
        },
        "content-hash-v1",
    )
    with db.connect() as conn:
        conn.execute("UPDATE notices SET fetched_date = '2026-10-01'")


def test_send_service_delivers_historical_pending_notice(tmp_path, monkeypatch) -> None:
    _configure_temp_database(tmp_path, monkeypatch)
    _insert_historical_notice()
    sent_messages = []

    async def fake_summarize(title: str, content_html: str) -> str:
        return f"摘要：{title}"

    def fake_send_digest(notices: list[dict], date_text: str, message_id: str) -> None:
        sent_messages.append((notices, date_text, message_id))

    async def fake_to_thread(function, *args):
        return function(*args)

    monkeypatch.setattr(service, "summarize", fake_summarize)
    monkeypatch.setattr(service, "send_digest", fake_send_digest)
    monkeypatch.setattr(service.asyncio, "to_thread", fake_to_thread)

    assert _run_immediate(service.send_pending_today()) == 1
    assert db.get_unsent_notices() == []
    assert len(sent_messages) == 1
    assert sent_messages[0][0][0]["summary"] == "摘要：历史待发通知"
    with db.connect() as conn:
        delivery = conn.execute("SELECT * FROM mail_deliveries").fetchone()
    assert delivery["status"] == "sent"
    assert delivery["attempts"] == 1
    assert delivery["last_error"] == ""


def test_send_service_records_failure_and_keeps_notice_pending(tmp_path, monkeypatch) -> None:
    _configure_temp_database(tmp_path, monkeypatch)
    _insert_historical_notice()

    async def fake_summarize(title: str, content_html: str) -> str:
        return title

    def failing_send_digest(notices: list[dict], date_text: str, message_id: str) -> None:
        raise TimeoutError("SMTP timeout")

    async def fake_to_thread(function, *args):
        return function(*args)

    monkeypatch.setattr(service, "summarize", fake_summarize)
    monkeypatch.setattr(service, "send_digest", failing_send_digest)
    monkeypatch.setattr(service.asyncio, "to_thread", fake_to_thread)

    with pytest.raises(TimeoutError, match="SMTP timeout"):
        _run_immediate(service.send_pending_today())

    assert len(db.get_unsent_notices()) == 1
    with db.connect() as conn:
        delivery = conn.execute("SELECT * FROM mail_deliveries").fetchone()
    assert delivery["status"] == "failed"
    assert delivery["attempts"] == 1
    assert "SMTP timeout" in delivery["last_error"]


def test_daily_run_alerts_when_cas_session_expires_and_continues(monkeypatch) -> None:
    alerts = []
    pending_calls = []

    async def expired_scrape(headless: bool):
        raise SessionExpiredError("会话已过期")

    async def fake_send_pending() -> int:
        pending_calls.append(True)
        return 0

    async def fake_to_thread(function, *args):
        return function(*args)

    def fake_alert(title: str, detail: str) -> None:
        alerts.append((title, detail))

    monkeypatch.setattr(service, "init_db", lambda: None)
    monkeypatch.setattr(service, "scrape_notices", expired_scrape)
    monkeypatch.setattr(service, "send_pending_today", fake_send_pending)
    monkeypatch.setattr(service, "send_system_alert", fake_alert)
    monkeypatch.setattr(service.asyncio, "to_thread", fake_to_thread)

    assert _run_immediate(service.run_daily()) is None
    assert pending_calls == [True]
    assert alerts[0][0] == "CAS 会话已过期"
    assert "init-auth" in alerts[0][1]


def test_daily_run_alerts_and_reraises_send_failure(monkeypatch) -> None:
    alerts = []

    async def successful_scrape(headless: bool) -> ScrapeStats:
        return ScrapeStats()

    async def failed_send_pending() -> int:
        raise RuntimeError("send failed")

    async def fake_to_thread(function, *args):
        return function(*args)

    def fake_alert(title: str, detail: str) -> None:
        alerts.append((title, detail))

    monkeypatch.setattr(service, "init_db", lambda: None)
    monkeypatch.setattr(service, "scrape_notices", successful_scrape)
    monkeypatch.setattr(service, "send_pending_today", failed_send_pending)
    monkeypatch.setattr(service, "send_system_alert", fake_alert)
    monkeypatch.setattr(service.asyncio, "to_thread", fake_to_thread)

    with pytest.raises(RuntimeError, match="send failed"):
        _run_immediate(service.run_daily())
    assert alerts == [("日报发送任务失败", "RuntimeError: send failed")]
