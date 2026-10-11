from __future__ import annotations

import pytest

import db
from tests.manual_email import sender
from tests.test_service import _configure_temp_database, _insert_historical_notice, _run_immediate


@pytest.mark.parametrize("smtp_fails", [False, True])
def test_test_email_preserves_database_state(tmp_path, monkeypatch, smtp_fails):
    _configure_temp_database(tmp_path, monkeypatch)
    monkeypatch.setattr(sender, "settings", db.settings)
    sender_settings = type(db.settings)(**vars(db.settings), my_email="administrator@example.test")
    monkeypatch.setattr(sender, "settings", sender_settings)
    _insert_historical_notice()
    with db.connect() as conn:
        before = conn.iterdump()
        before = list(before)
    messages = []

    async def summarize(title, content):
        return "测试摘要"

    def send(notices, date_text, message_id, **kwargs):
        messages.append((notices, kwargs))
        if smtp_fails:
            raise OSError("SMTP unavailable")

    async def to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(sender, "summarize", summarize)
    monkeypatch.setattr(sender, "send_digest", send)
    monkeypatch.setattr(sender.asyncio, "to_thread", to_thread)
    if smtp_fails:
        with pytest.raises(OSError, match="SMTP unavailable"):
            _run_immediate(sender.send_test_email())
    else:
        assert _run_immediate(sender.send_test_email()) == 1
    assert messages[0][0][0]["summary"] == "测试摘要"
    assert messages[0][1]["subject_prefix"] == "【测试邮件】"
    with db.connect() as conn:
        assert list(conn.iterdump()) == before


def test_test_notice_prefers_attachments_and_supports_id(tmp_path, monkeypatch):
    _configure_temp_database(tmp_path, monkeypatch)
    monkeypatch.setattr(sender, "settings", db.settings)
    _insert_historical_notice()
    with db.connect() as conn:
        conn.execute("UPDATE notices SET attachments_json = ?", ('[{"name":"report.pdf"}]',))
    db.save_notice(
        dict(
            title="新通知",
            detail_url="https://example.test/new",
            content_html="正文",
            publish_time="",
            department="",
            attachments=[],
        ),
        "new-hash",
    )
    assert sender.get_test_notice()["title"] == "历史待发通知"
    assert sender.get_test_notice(2)["title"] == "新通知"
    assert sender.get_test_notice(999) is None


@pytest.mark.parametrize("status", ["paused", "unsubscribed"])
def test_manual_test_cannot_bypass_disabled_recipient(tmp_path, monkeypatch, status):
    _configure_temp_database(tmp_path, monkeypatch)
    monkeypatch.setattr(
        sender,
        "settings",
        type(db.settings)(**vars(db.settings), my_email="administrator@example.test"),
    )
    user_id = db.add_user("administrator@example.test", consent_source="explicit-consent")
    db.set_user_status(user_id, status)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("disabled recipient must not receive test mail")

    monkeypatch.setattr(sender, "summarize", forbidden)
    monkeypatch.setattr(sender, "send_digest", forbidden)
    with pytest.raises(ValueError, match="暂停或退订"):
        _run_immediate(sender.send_test_email())
