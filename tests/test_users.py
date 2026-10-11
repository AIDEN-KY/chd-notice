from __future__ import annotations

from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import db


@pytest.mark.parametrize("pause_consent", ["", "new-consent-on-invalid-transition"])
def test_unsubscribe_cannot_be_bypassed_via_pause_then_resume(tmp_path, monkeypatch, pause_consent):
    """退订是撤回同意，不得经暂停状态洗掉重新订阅的同意要求。"""
    monkeypatch.setattr(
        db,
        "settings",
        SimpleNamespace(database_file=tmp_path / "notices.db", timezone=ZoneInfo("Asia/Shanghai")),
    )
    db.init_db()
    user_id = db.add_user("student@example.test", consent_source="original-consent")
    db.set_user_status(user_id, "unsubscribed")
    before = db.get_users()[0]

    with pytest.raises(ValueError):
        db.set_user_status(user_id, "paused", consent_source=pause_consent)
        db.set_user_status(user_id, "active")

    assert db.get_users()[0] == before, "非法状态转换不能覆盖退订或原同意记录"
    assert db.get_active_users() == []
    # 正常重新订阅仍然可用，只接受明确恢复动作上的新同意。
    db.save_notice(
        {
            "title": "退订期间的通知",
            "detail_url": "https://example.test/during-unsubscribe",
            "content_html": "<p>历史正文</p>",
            "attachments": [],
        },
        "during-unsubscribe-hash",
    )
    db.set_user_status(user_id, "active", consent_source="renewed-consent")
    active = db.get_active_users()[0]
    assert active["consent_source"] == "renewed-consent"
    assert active["start_after_notice_id"] == 1


def test_users_migration_is_repeatable_and_preserves_existing_data(tmp_path, monkeypatch):
    monkeypatch.setattr(
        db,
        "settings",
        SimpleNamespace(database_file=tmp_path / "notices.db", timezone=ZoneInfo("Asia/Shanghai")),
    )
    db.init_db()
    db.save_notice(
        {
            "title": "Existing notice",
            "detail_url": "https://example.test/notice",
            "content_html": "Original content",
            "attachments": [],
        },
        "original-hash",
    )
    db.ensure_delivery("existing-delivery", "<existing@example.test>", [1])
    with db.connect() as conn:
        original_notice = dict(conn.execute("SELECT * FROM notices").fetchone())
        original_delivery = dict(conn.execute("SELECT * FROM mail_deliveries").fetchone())
        # 模拟已有单人版本：通知和投递存在，但尚无用户表。
        conn.execute("DROP TABLE IF EXISTS users")
    db.init_db()
    db.init_db()
    with db.connect() as conn:
        assert dict(conn.execute("SELECT * FROM notices").fetchone()) == original_notice
        assert dict(conn.execute("SELECT * FROM mail_deliveries").fetchone()) == original_delivery
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        assert {
            "id",
            "email",
            "email_key",
            "status",
            "created_at",
            "updated_at",
            "subscribed_at",
            "consent_source",
            "start_after_notice_id",
        } <= columns, "users schema must exist after repeatable migration"
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_users_registration_is_explicit_and_paused_users_are_excluded(tmp_path, monkeypatch):
    monkeypatch.setattr(
        db,
        "settings",
        SimpleNamespace(database_file=tmp_path / "notices.db", timezone=ZoneInfo("Asia/Shanghai")),
    )
    monkeypatch.setenv("MY_EMAIL", "administrator@example.test")
    db.init_db()
    # Proposed public API: registration returns the user ID; state changes retain the record.
    assert db.get_active_users() == [], "initialization must not implicitly subscribe MY_EMAIL"
    first_id = db.add_user("first@example.test", consent_source="explicit-test-consent")
    second_id = db.add_user("second@example.test", consent_source="explicit-test-consent")
    db.set_user_status(second_id, "paused")
    assert [user["id"] for user in db.get_active_users()] == [first_id]
    db.init_db()
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2
        assert (
            conn.execute("SELECT status FROM users WHERE id = ?", (second_id,)).fetchone()[0]
            == "paused"
        )


@pytest.mark.parametrize(
    "email",
    ["a\r\nBcc:b@example.test", "a@example.test,b@example.test", "bad", "a@", "ü@example.test"],
)
def test_user_rejects_invalid_email(tmp_path, monkeypatch, email):
    monkeypatch.setattr(
        db,
        "settings",
        SimpleNamespace(database_file=tmp_path / "notices.db", timezone=ZoneInfo("Asia/Shanghai")),
    )
    db.init_db()
    with pytest.raises(ValueError):
        db.add_user(email, consent_source="explicit-consent")


def test_duplicate_user_does_not_reactivate_or_reset_history(tmp_path, monkeypatch):
    monkeypatch.setattr(
        db,
        "settings",
        SimpleNamespace(database_file=tmp_path / "notices.db", timezone=ZoneInfo("Asia/Shanghai")),
    )
    db.init_db()
    user_id = db.add_user("Student@EXAMPLE.test", consent_source="explicit-consent")
    db.set_user_status(user_id, "unsubscribed")
    assert db.add_user("Student@example.test", consent_source="explicit-consent") == user_id
    assert db.get_active_users() == []


def test_no_users_skips_summary_smtp_and_preserves_pending(tmp_path, monkeypatch):
    import service
    from tests.test_service import (
        _configure_temp_database,
        _insert_historical_notice,
        _run_immediate,
    )

    _configure_temp_database(tmp_path, monkeypatch)
    _insert_historical_notice()
    monkeypatch.setattr(service, "get_active_users", db.get_active_users)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("no subscribers must not invoke external services")

    monkeypatch.setattr(service, "summarize", forbidden)
    monkeypatch.setattr(service, "send_digest", forbidden)
    assert _run_immediate(service.send_pending_today()) == 0
    assert len(db.get_unsent_notices()) == 1


def test_unsubscribe_requires_new_consent_to_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(
        db,
        "settings",
        SimpleNamespace(database_file=tmp_path / "notices.db", timezone=ZoneInfo("Asia/Shanghai")),
    )
    db.init_db()
    with pytest.raises(ValueError, match="同意"):
        db.add_user("student@example.test", consent_source="")
    user_id = db.add_user("student@example.test", consent_source="initial-consent")
    db.set_user_status(user_id, "unsubscribed")
    with pytest.raises(ValueError, match="同意"):
        db.set_user_status(user_id, "active")
    db.set_user_status(user_id, "active", consent_source="renewed-consent")
    assert db.get_active_users()[0]["consent_source"] == "renewed-consent"
    with pytest.raises(ValueError):
        db.set_user_status(user_id, "invalid")
    with pytest.raises(ValueError):
        db.set_user_status(999, "paused")


def test_group_delivery_stays_closed_until_remaining_safety_work(tmp_path, monkeypatch):
    import service
    from tests.test_service import (
        _configure_temp_database,
        _insert_historical_notice,
        _run_immediate,
    )

    _configure_temp_database(tmp_path, monkeypatch)
    db.add_user("receiver@example.test", consent_source="explicit-test-consent")
    _insert_historical_notice()
    monkeypatch.setattr(
        service,
        "settings",
        SimpleNamespace(timezone=ZoneInfo("Asia/Shanghai"), multi_user_delivery_ready=False),
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("closed delivery must not call summary or SMTP")

    monkeypatch.setattr(service, "summarize", forbidden)
    monkeypatch.setattr(service, "send_digest", forbidden)
    assert _run_immediate(service.send_pending_today()) == 0
    assert len(db.get_unsent_notices()) == 1


def test_users_cli_add_and_pause_are_local_only(tmp_path, monkeypatch, capsys):
    import sys

    import main

    monkeypatch.setattr(
        db,
        "settings",
        SimpleNamespace(database_file=tmp_path / "notices.db", timezone=ZoneInfo("Asia/Shanghai")),
    )
    monkeypatch.setattr(main, "configure_logging", lambda: None)

    def forbidden_scheduler():
        raise AssertionError("users command must not start scheduler")

    monkeypatch.setattr(main, "run_scheduler", forbidden_scheduler)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "main.py",
            "users",
            "--action",
            "add",
            "--email",
            "student@example.test",
            "--consent-source",
            "explicit-consent",
        ],
    )
    main.main()
    assert len(db.get_active_users()) == 1
    monkeypatch.setattr(sys, "argv", ["main.py", "users", "--action", "pause", "--user-id", "1"])
    main.main()
    assert db.get_active_users() == []
    assert "student@example.test" not in capsys.readouterr().out
