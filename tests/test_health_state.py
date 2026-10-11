"""健康与恢复契约：假门户、假 SMTP、临时数据库和会话文件。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

import db
import scraper
import service
from tests.test_service import _configure_temp_database, _run_immediate


def test_repeated_admin_alert_is_suppressed_during_cooldown(tmp_path, monkeypatch):
    _configure_temp_database(tmp_path, monkeypatch)
    sent = []

    async def immediate_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(service.asyncio, "to_thread", immediate_thread)
    monkeypatch.setattr(service, "send_system_alert", lambda *args: sent.append(args))
    _run_immediate(service._try_send_alert("CAS 会话已过期", "请手动登录"))
    _run_immediate(service._try_send_alert("CAS 会话已过期", "请手动登录"))
    assert len(sent) == 1, "相同故障不能在冷却期重复给管理员发信"


def test_failed_scrape_with_successful_inventory_send_returns_degraded(tmp_path, monkeypatch):
    _configure_temp_database(tmp_path, monkeypatch)
    sends = []

    async def expired_scrape(**_kwargs):
        raise scraper.SessionExpiredError("会话已过期")

    async def fake_pending():
        sends.append(True)
        return 1

    async def fake_alert(*_args):
        pass

    monkeypatch.setattr(service, "scrape_notices", expired_scrape)
    monkeypatch.setattr(service, "_send_pending_today", fake_pending)
    monkeypatch.setattr(service, "_try_send_alert", fake_alert)
    result = _run_immediate(service.run_daily())
    assert sends == [True]
    # 固定返回码契约：0=正常；3=抓取失败但库存处理完成，CLI 需透传。
    assert result == 3, "不能把抓取失败的降级任务报告成正常成功"
    health = db.get_task_health()
    assert health["latest"]["status"] == "degraded"
    assert health["latest"]["finished_at"]
    assert health["latest"]["delivered_count"] == 1
    assert health["last_successful_scrape_at"] is None


def test_alert_failure_is_persisted_and_cooled_down_without_secret_logging(
    tmp_path, monkeypatch, caplog
):
    _configure_temp_database(tmp_path, monkeypatch)
    attempts = []

    def failed_smtp(*_args):
        attempts.append(True)
        raise TimeoutError("secret-email@example.test secret-key")

    async def immediate_thread(function, *args):
        return function(*args)

    monkeypatch.setattr(service, "send_system_alert", failed_smtp)
    monkeypatch.setattr(service.asyncio, "to_thread", immediate_thread)
    _run_immediate(service._try_send_alert("通知抓取失败", "RuntimeError"))
    _run_immediate(service._try_send_alert("通知抓取失败", "RuntimeError"))
    with db.connect() as conn:
        alert = conn.execute("SELECT * FROM admin_alerts").fetchone()
    assert attempts == [True]
    assert alert["status"] == "failed"
    assert alert["error_type"] == "TimeoutError"
    assert "secret-key" not in caplog.text
    assert "secret-email" not in caplog.text


def test_health_records_success_and_preserves_last_success_on_later_failure(tmp_path, monkeypatch):
    _configure_temp_database(tmp_path, monkeypatch)

    async def successful_scrape(**_kwargs):
        return scraper.ScrapeStats()

    async def pending():
        return 0

    monkeypatch.setattr(service, "scrape_notices", successful_scrape)
    monkeypatch.setattr(service, "_send_pending_today", pending)
    assert _run_immediate(service.run_daily()) == 0
    success = db.get_task_health()
    assert success["latest"]["status"] == "success"
    assert success["last_successful_scrape_at"]
    run_id = db.start_task_run()
    db.finish_task_run(run_id, status="failed", scrape_succeeded=False, error_type="RuntimeError")
    assert db.get_task_health()["last_successful_scrape_at"] == success["last_successful_scrape_at"]


def test_auth_replace_failure_keeps_old_session_and_removes_temporary_file(tmp_path, monkeypatch):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("old-session", encoding="utf-8")
    monkeypatch.setattr(scraper, "settings", SimpleNamespace(auth_file=auth_file))

    async def storage_state():
        return {"cookies": [], "origins": []}

    def failed_replace(*_args):
        raise OSError("simulated atomic replacement failure")

    monkeypatch.setattr(scraper.os, "replace", failed_replace)
    with pytest.raises(OSError):
        _run_immediate(
            scraper._save_auth_state_atomic(SimpleNamespace(storage_state=storage_state))
        )
    assert auth_file.read_text(encoding="utf-8") == "old-session"
    assert list(tmp_path.glob(".auth-*.tmp")) == []


def test_successful_auth_state_replacement_is_complete(tmp_path, monkeypatch):
    import json

    auth_file = tmp_path / "auth.json"
    auth_file.write_text("old-session", encoding="utf-8")
    monkeypatch.setattr(scraper, "settings", SimpleNamespace(auth_file=auth_file))

    async def storage_state():
        return {"cookies": [{"name": "fake", "value": "fake"}], "origins": []}

    _run_immediate(scraper._save_auth_state_atomic(SimpleNamespace(storage_state=storage_state)))
    assert json.loads(auth_file.read_text(encoding="utf-8")) == _run_immediate(storage_state())
    assert list(tmp_path.glob(".auth-*.tmp")) == []


@pytest.mark.parametrize("failure_stage", ["storage", "validation"])
def test_auth_write_failure_preserves_previous_session_and_closes_browser(
    tmp_path, monkeypatch, failure_stage
):
    auth_file = tmp_path / "auth.json"
    previous = '{"cookies": [{"name": "old-session"}], "origins": []}'
    auth_file.write_text(previous, encoding="utf-8")
    closed = []
    page = SimpleNamespace(url="https://portal.example.test/notice")

    async def new_page():
        return page

    async def storage_state(path=None, **_kwargs):
        if path is not None:
            Path(path).write_text('{"cookies":', encoding="utf-8")
        raise OSError("simulated storage failure")

    async def close_context():
        closed.append("context")

    context = SimpleNamespace(new_page=new_page, storage_state=storage_state, close=close_context)

    async def new_context(**_kwargs):
        return context

    async def close_browser():
        closed.append("browser")

    browser = SimpleNamespace(new_context=new_context, close=close_browser)

    async def launch(**_kwargs):
        return browser

    class Playwright:
        async def __aenter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(launch=launch))

        async def __aexit__(self, *_args):
            pass

    async def noop(*_args, **_kwargs):
        pass

    async def not_login(*_args):
        return False

    async def visible_list(*_args, **_kwargs):
        return ".fake-list" if failure_stage == "storage" else None

    monkeypatch.setattr(
        scraper, "settings", SimpleNamespace(auth_file=auth_file, notices_url=page.url)
    )
    monkeypatch.setattr(scraper, "async_playwright", Playwright)
    monkeypatch.setattr(scraper, "_goto_with_retry", noop)
    monkeypatch.setattr(scraper, "_is_login_page", not_login)
    monkeypatch.setattr(scraper, "_wait_for_list", visible_list)
    monkeypatch.setattr(scraper.asyncio, "to_thread", noop)
    expected = OSError if failure_stage == "storage" else RuntimeError
    with pytest.raises(expected):
        _run_immediate(scraper.initialize_auth())
    assert auth_file.read_text(encoding="utf-8") == previous
    assert closed == ["context", "browser"]


def test_send_now_cli_returns_degraded_exit_code(tmp_path, monkeypatch):
    import sys

    import main

    _configure_temp_database(tmp_path, monkeypatch)

    async def degraded_daily():
        return 3

    monkeypatch.setattr(main, "configure_logging", lambda: None)
    monkeypatch.setattr(main, "run_daily", degraded_daily)
    monkeypatch.setattr(main.asyncio, "run", _run_immediate)
    monkeypatch.setattr(sys, "argv", ["main.py", "send-now"])
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 3
