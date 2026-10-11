from __future__ import annotations

from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import db
import mailer
import service
from tests.test_service import _run_immediate


@pytest.fixture
def delivery_setup(tmp_path, monkeypatch):
    configuration = SimpleNamespace(
        database_file=tmp_path / "notices.db",
        timezone=ZoneInfo("Asia/Shanghai"),
        # 仅在离线测试中解除发布保护，证明内部投递缺陷；不改正式配置。
        multi_user_delivery_ready=True,
        smtp_host="smtp.example.test",
        smtp_port=465,
        qq_email="sender@example.test",
        qq_smtp_auth_code="fake-code",
        my_email="admin@example.test",
        unsubscribe_url="",
        max_attachment_bytes=20 * 1024 * 1024,
    )
    for module in (db, service, mailer):
        monkeypatch.setattr(module, "settings", configuration)
    db.init_db()
    monkeypatch.setattr(service, "get_active_users", db.get_active_users)

    async def summarize(title, content):
        return "Fixed summary"

    async def to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(service, "summarize", summarize)
    monkeypatch.setattr(service.asyncio, "to_thread", to_thread)
    return monkeypatch


def add_notice(url="https://example.test/current"):
    db.save_notice(
        {"title": "Notice", "detail_url": url, "content_html": "Content", "attachments": []},
        "hash-v1",
    )


def register_pair():
    return [
        db.add_user(address, consent_source="explicit-consent")
        for address in ("a@example.test", "b@example.test")
    ]


def test_partial_smtp_refusal_preserves_accepted_recipient_result(delivery_setup):
    register_pair()
    add_notice()
    envelopes = []

    class FakeSMTP:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def login(self, *_args):
            pass

        def send_message(self, message, *, from_addr, to_addrs):
            envelopes.append(list(to_addrs))
            assert message["Bcc"] is None
            assert message["Cc"] is None
            for email in to_addrs:
                assert email.encode() not in message.as_bytes()
            return {"b@example.test": (450, b"temporary refusal")}

    delivery_setup.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTP)
    # A 已被 SMTP 接受，B 明确临时拒收，不应丢失 A 的成功事实或整体抛普通异常。
    _run_immediate(service.send_pending_today())
    assert envelopes == [["a@example.test", "b@example.test"]]
    with db.connect() as conn:
        states = {
            row["email"]: (row["status"], row["smtp_code"])
            for row in conn.execute("SELECT * FROM batch_recipients")
        }
    assert states == {"a@example.test": ("accepted", None), "b@example.test": ("refused", 450)}


def test_retry_targets_only_explicitly_refused_recipient(delivery_setup):
    register_pair()
    add_notice()
    envelopes = []

    def send(notices, date_text, message_id, *, recipients):
        envelopes.append(list(recipients))
        # Proposed transport result contract: accepted vs explicitly refused envelope.
        if len(envelopes) == 1:
            return {
                "accepted": ["a@example.test"],
                "refused": {"b@example.test": (450, b"temporary refusal")},
            }
        return {"accepted": list(recipients), "refused": {}}

    delivery_setup.setattr(service, "send_digest", send)
    _run_immediate(service.send_pending_today())
    _run_immediate(service.send_pending_today())
    assert envelopes == [["a@example.test", "b@example.test"], ["b@example.test"]], (
        "retry must neither lose B nor resend to accepted A"
    )
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM digest_batches").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM user_notice_deliveries WHERE status='accepted'"
            ).fetchone()[0]
            == 2
        )


def test_new_subscriber_does_not_receive_pre_subscription_history(delivery_setup):
    add_notice("https://example.test/history")
    db.add_user("a@example.test", consent_source="explicit-consent")
    add_notice("https://example.test/new")
    delivered_urls = []

    def send(notices, date_text, message_id, *, recipients):
        delivered_urls.extend(notice["detail_url"] for notice in notices)
        return {"accepted": list(recipients), "refused": {}}

    delivery_setup.setattr(service, "send_digest", send)
    _run_immediate(service.send_pending_today())
    assert delivered_urls == ["https://example.test/new"]


def test_unsubscribed_during_summary_is_excluded_before_smtp(delivery_setup):
    _, second_id = register_pair()
    add_notice()
    envelopes = []

    async def summarize(title, content):
        db.set_user_status(second_id, "unsubscribed")
        return "Fixed summary"

    def send(notices, date_text, message_id, *, recipients):
        envelopes.append(list(recipients))
        return {"accepted": list(recipients), "refused": {}}

    delivery_setup.setattr(service, "summarize", summarize)
    delivery_setup.setattr(service, "send_digest", send)
    _run_immediate(service.send_pending_today())
    assert envelopes == [["a@example.test"]], "SMTP must recheck subscription state"


def test_timeout_unknown_delivery_is_not_automatically_resent(delivery_setup):
    register_pair()
    add_notice()
    attempts = []

    def send(*_args, **_kwargs):
        attempts.append(True)
        raise TimeoutError("SMTP outcome unknown")

    delivery_setup.setattr(service, "send_digest", send)
    for _ in range(2):
        try:
            _run_immediate(service.send_pending_today())
        except TimeoutError:
            pass
    assert len(attempts) == 1, "unknown SMTP result must await explicit recovery, not auto retry"
    with db.connect() as conn:
        assert {row[0] for row in conn.execute("SELECT status FROM batch_recipients")} == {
            "unknown"
        }


@pytest.mark.parametrize(
    "code,expected_attempts,expected_status", [(450, 3, "refused"), (550, 1, "permanent")]
)
def test_all_refused_retries_are_bounded_and_permanent_refusals_stop(
    delivery_setup, code, expected_attempts, expected_status
):
    register_pair()
    add_notice()
    attempts = []

    def send(notices, date_text, message_id, *, recipients):
        attempts.append(list(recipients))
        return {"accepted": [], "refused": {address: (code, b"refused") for address in recipients}}

    delivery_setup.setattr(service, "send_digest", send)
    for _ in range(5):
        _run_immediate(service.send_pending_today())
    assert len(attempts) == expected_attempts
    with db.connect() as conn:
        assert {row[0] for row in conn.execute("SELECT status FROM batch_recipients")} == {
            expected_status
        }
        assert conn.execute("SELECT sent FROM notices").fetchone()[0] == 0


def test_retry_uses_original_snapshot_even_after_notice_update(delivery_setup):
    register_pair()
    add_notice()
    snapshots = []
    summaries = []

    async def summarize(title, content):
        summaries.append(content)
        return "Cached summary"

    def send(notices, date_text, message_id, *, recipients):
        snapshots.append((list(recipients), notices[0]["content_hash"], notices[0]["content_html"]))
        if len(snapshots) == 1:
            return {"accepted": ["a@example.test"], "refused": {"b@example.test": (450, b"retry")}}
        return {"accepted": recipients, "refused": {}}

    delivery_setup.setattr(service, "summarize", summarize)
    delivery_setup.setattr(service, "send_digest", send)
    _run_immediate(service.send_pending_today())
    db.save_notice(
        {
            "title": "Notice",
            "detail_url": "https://example.test/current",
            "content_html": "Updated",
            "attachments": [],
        },
        "hash-v2",
    )
    _run_immediate(service.send_pending_today())
    assert snapshots[1] == (["b@example.test"], "hash-v1", "Content")
    assert snapshots[2] == (["a@example.test", "b@example.test"], "hash-v2", "Updated")
    assert summaries == ["Content", "Updated"]


def test_recipient_state_not_legacy_sent_controls_selection(delivery_setup):
    db.add_user("a@example.test", consent_source="explicit-consent")
    add_notice()
    with db.connect() as conn:
        conn.execute("UPDATE notices SET sent=1")
    envelopes = []

    def send(notices, date_text, message_id, *, recipients):
        envelopes.append(recipients)
        return {"accepted": recipients, "refused": {}}

    delivery_setup.setattr(service, "send_digest", send)
    _run_immediate(service.send_pending_today())
    _run_immediate(service.send_pending_today())
    assert envelopes == [["a@example.test"]]


def test_malformed_transport_result_is_unknown_not_success(delivery_setup):
    register_pair()
    add_notice()
    delivery_setup.setattr(
        service,
        "send_digest",
        lambda *_args, **_kwargs: {"accepted": ["a@example.test"], "refused": {}},
    )
    with pytest.raises(ValueError, match="envelope"):
        _run_immediate(service.send_pending_today())
    with db.connect() as conn:
        assert conn.execute("SELECT sent FROM notices").fetchone()[0] == 0
        assert {row[0] for row in conn.execute("SELECT status FROM batch_recipients")} == {
            "unknown"
        }


def test_recipient_batch_schema_is_repeatable_without_losing_users(delivery_setup):
    register_pair()
    add_notice()
    db.init_db()
    db.init_db()
    with db.connect() as conn:
        tables = {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"digest_batches", "batch_notices", "batch_recipients"} <= tables
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM notices").fetchone()[0] == 1


def test_explicit_history_authorization_preserves_existing_acceptances(delivery_setup):
    add_notice("https://example.test/history")
    user_id = db.add_user("a@example.test", consent_source="explicit-consent")
    add_notice("https://example.test/new")
    batches = []

    def send(notices, date_text, message_id, *, recipients):
        batches.append([n["detail_url"] for n in notices])
        return {"accepted": recipients, "refused": {}}

    delivery_setup.setattr(service, "send_digest", send)
    _run_immediate(service.send_pending_today())
    with pytest.raises(ValueError):
        db.authorize_history(user_id, reason="")
    db.authorize_history(user_id, reason="user explicitly requested historical notices")
    _run_immediate(service.send_pending_today())
    _run_immediate(service.send_pending_today())
    assert batches == [["https://example.test/new"], ["https://example.test/history"]]


def test_all_recipients_refused_exception_is_recorded_as_explicit_failure(delivery_setup):
    import smtplib

    register_pair()
    add_notice()

    class FakeSMTP:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def login(self, *_args):
            pass

        def send_message(self, message, *, from_addr, to_addrs):
            raise smtplib.SMTPRecipientsRefused(
                {address: (550, b"no such user") for address in to_addrs}
            )

    delivery_setup.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTP)
    _run_immediate(service.send_pending_today())
    with db.connect() as conn:
        assert {row[0] for row in conn.execute("SELECT status FROM batch_recipients")} == {
            "permanent"
        }
        before = list(conn.iterdump())
    db.init_db()
    db.init_db()
    with db.connect() as conn:
        assert list(conn.iterdump()) == before
