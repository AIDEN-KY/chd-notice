from __future__ import annotations

from types import SimpleNamespace

import pytest

import db
from tests.manual_email import sender
from tests.test_service import _configure_temp_database, _insert_historical_notice, _run_immediate


def test_bcc_trial_real_summarizer_does_not_write_formal_cache(tmp_path, monkeypatch):
    import summarizer

    _configure_temp_database(tmp_path, monkeypatch)
    configuration = SimpleNamespace(
        **vars(db.settings),
        my_email="owner@example.test",
        qq_email="sender@example.test",
        ai_summary_enabled=True,
        ai_excluded_keywords=("禁止转发",),
        deepseek_model="fake-model",
        deepseek_url="https://example.test/api",
        deepseek_api_key="fake-key",
        retry_attempts=1,
        retry_backoff_seconds=0,
    )
    monkeypatch.setattr(sender, "settings", configuration)
    monkeypatch.setattr(summarizer, "settings", configuration)
    monkeypatch.setattr(sender, "summarize", summarizer.summarize)

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": "本通知为合成测试，用于检查双邮箱密送和摘要流程，不代表学校真实活动，请检查邮件标题摘要详情链接的显示情况并确认收件人地址未泄露。"
                        }
                    }
                ]
            }

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, *_args, **_kwargs):
            return Response()

    async def thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(summarizer.httpx, "AsyncClient", Client)
    monkeypatch.setattr(sender.asyncio, "to_thread", thread)
    monkeypatch.setattr(
        sender, "send_digest", lambda *_args, **kw: {"accepted": kw["recipients"], "refused": {}}
    )
    with db.connect() as conn:
        before = list(conn.iterdump())
    assert _run_immediate(sender.send_bcc_test_email(confirm_send=True)) == 2
    with db.connect() as conn:
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize("confirm", [False, True])
def test_isolated_bcc_trial_requires_confirmation_and_preserves_database(
    tmp_path, monkeypatch, confirm
):
    _configure_temp_database(tmp_path, monkeypatch)
    _insert_historical_notice()
    monkeypatch.setattr(
        sender,
        "settings",
        SimpleNamespace(
            **vars(db.settings), my_email="owner1@example.test", qq_email="owner2@example.test"
        ),
    )
    with db.connect() as conn:
        before = list(conn.iterdump())
    requests, envelopes = [], []

    async def summary(title, content, **kwargs):
        assert kwargs == {"use_cache": False}
        requests.append((title, content))
        assert "合成" in content
        return "地点：测试报告厅；联系方式：测试联系人。"

    def smtp(notices, *_args, **kwargs):
        assert notices[0]["attachments"] == []
        assert notices[0]["detail_url"] == "https://example.com/"
        envelopes.append(kwargs["recipients"])
        return {"accepted": kwargs["recipients"], "refused": {}}

    async def thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(sender, "summarize", summary)
    monkeypatch.setattr(sender, "send_digest", smtp)
    monkeypatch.setattr(sender.asyncio, "to_thread", thread)
    if confirm:
        assert _run_immediate(sender.send_bcc_test_email(confirm_send=True)) == 2
        assert len(requests) == 1
        assert envelopes == [["owner1@example.test", "owner2@example.test"]]
    else:
        with pytest.raises(ValueError):
            _run_immediate(sender.send_bcc_test_email())
        assert requests == envelopes == []
    with db.connect() as conn:
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize("case", ["same", "paused", "unsubscribed", "refused", "failure"])
def test_bcc_trial_rejects_unsafe_targets_and_failed_delivery(tmp_path, monkeypatch, case):
    _configure_temp_database(tmp_path, monkeypatch)
    monkeypatch.setattr(
        sender,
        "settings",
        SimpleNamespace(
            **vars(db.settings),
            my_email="owner@example.test",
            qq_email="owner@example.test" if case == "same" else "sender@example.test",
        ),
    )
    if case in {"paused", "unsubscribed"}:
        user_id = db.add_user("owner@example.test", consent_source="test-consent")
        db.set_user_status(user_id, case)
    summary_calls = []

    async def summary(*_args, **_kwargs):
        summary_calls.append(True)
        return "合成摘要"

    def smtp(*_args, **kwargs):
        if case == "failure":
            raise OSError("PRIVATE_SMTP_MARKER")
        return {
            "accepted": kwargs["recipients"][:1],
            "refused": {"sender@example.test": (450, b"refused")},
        }

    async def thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(sender, "summarize", summary)
    monkeypatch.setattr(sender, "send_digest", smtp)
    monkeypatch.setattr(sender.asyncio, "to_thread", thread)
    with pytest.raises((ValueError, RuntimeError)) as exc:
        _run_immediate(sender.send_bcc_test_email(confirm_send=True))
    assert "PRIVATE_SMTP_MARKER" not in str(exc.value)
    if case in {"same", "paused", "unsubscribed"}:
        assert summary_calls == []


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
