"""只读 preview 命令契约：不使用真实数据库、门户、API 或 SMTP。"""

import json
import sys

import pytest

import db
import main
import service
import summarizer
from tests.test_delivery_recipients import add_notice, register_pair
from tests.test_delivery_recipients import delivery_setup as _delivery_setup

delivery_setup = _delivery_setup


def _run_preview(monkeypatch, capsys):
    monkeypatch.setattr(main, "settings", service.settings)
    monkeypatch.setattr(sys, "argv", ["main.py", "preview"])

    def forbidden(*_args, **_kwargs):
        pytest.fail("preview 不得初始化数据库、生成摘要、抓取或发送邮件")

    monkeypatch.setattr(main, "init_db", forbidden)
    monkeypatch.setattr(main, "configure_logging", forbidden)
    monkeypatch.setattr(main, "scrape_notices", forbidden)
    monkeypatch.setattr(main, "send_pending_today", forbidden)
    monkeypatch.setattr(main, "run_daily", forbidden)
    monkeypatch.setattr(service, "summarize", forbidden)
    monkeypatch.setattr(service, "send_digest", forbidden)
    monkeypatch.setattr(summarizer.httpx, "AsyncClient", forbidden)
    try:
        main.main()
    except SystemExit as exc:
        assert exc.code in (None, 0), "应提供 preview 命令；当前 CLI 拒绝该命令"
    return json.loads(capsys.readouterr().out)


def test_preview_reports_backlog_without_writes_or_network(delivery_setup, capsys):
    register_pair()
    for index in range(5):
        add_notice(f"https://example.test/backlog/{index}")
    service.settings.max_notices_per_digest = 2
    service.settings.max_digest_messages_per_day = 1
    path = service.settings.database_file
    before = path.read_bytes()
    with db.connect() as connection:
        logical_before = list(connection.iterdump())

    try:
        report = _run_preview(delivery_setup, capsys)
        assert report["active_users"] == 2
        assert report["pending_unique_notices"] == 5
        assert report["pending_recipient_notices"] == 10
        assert report["existing_pending_batches"] == 0
        assert report["max_notices_per_digest"] == 2
        assert report["max_digest_messages_per_day"] == 1
        assert report["estimated_attachment_bytes"] == 0
        assert report["history_policy"] == "subscription_watermark"
        serialized = json.dumps(report)
        assert "a@example.test" not in serialized
        assert "b@example.test" not in serialized
    finally:
        assert path.read_bytes() == before
        with db.connect() as connection:
            assert list(connection.iterdump()) == logical_before


def test_preview_missing_database_does_not_create_it(delivery_setup, tmp_path, capsys):
    service.settings.database_file = tmp_path / "not-created.db"
    path = service.settings.database_file
    report = _run_preview(delivery_setup, capsys)
    assert report["database_status"] == "missing"
    assert report["active_users"] == 0
    assert not path.exists()


def test_preview_counts_frozen_backlog_and_excludes_unknown(delivery_setup, capsys):
    users = register_pair()
    add_notice()
    db.prepare_recipient_batches(db.get_active_users())
    with db.connect() as connection:
        connection.execute(
            "UPDATE batch_recipients SET status='unknown' WHERE user_id=?", (users[0],)
        )
        connection.execute(
            "UPDATE user_notice_deliveries SET status='unknown' WHERE user_id=?", (users[0],)
        )
    before = service.settings.database_file.read_bytes()
    report = _run_preview(delivery_setup, capsys)
    assert report["pending_recipient_notices"] == 1
    assert report["pending_unique_notices"] == 1
    assert report["existing_pending_batches"] == 1
    assert report["unknown_recipient_batches"] == 1
    assert service.settings.database_file.read_bytes() == before


def test_preview_uses_metadata_without_opening_attachment_files(delivery_setup, capsys):
    register_pair()
    db.save_notice(
        {
            "title": "Notice",
            "detail_url": "https://example.test/attachment",
            "content_html": "Content",
            "attachments": [
                {"name": "fake.pdf", "size": 123, "path": "//never-connect.test/share/fake.pdf"},
                {"name": "unknown.pdf", "size": 0},
            ],
        },
        "hash-v1",
    )
    report = _run_preview(delivery_setup, capsys)
    assert report["estimated_attachment_bytes"] == 123
    assert report["attachments_with_unknown_size"] == 1
    assert report["attachment_size_basis"] == "stored_metadata_not_mime_size"


def test_preview_damaged_database_is_reported_without_repair(delivery_setup, capsys):
    path = service.settings.database_file
    path.write_bytes(b"not-a-sqlite-database")
    before = path.read_bytes()
    report = _run_preview(delivery_setup, capsys)
    assert report["database_status"] == "unreadable_or_incompatible"
    assert path.read_bytes() == before


def test_preview_single_user_release_requires_only_configured_owner(delivery_setup, capsys):
    service.settings.multi_user_delivery_ready = False
    service.settings.single_user_delivery_ready = True
    service.settings.my_email = "owner@example.test"
    db.add_user("owner@example.test", consent_source="owner-explicit-consent")
    report = _run_preview(delivery_setup, capsys)
    assert report["delivery_enabled"] is True
    assert report["delivery_mode"] == "single_my_email"

    db.add_user("other@example.test", consent_source="other-explicit-consent")
    report = _run_preview(delivery_setup, capsys)
    assert report["delivery_enabled"] is False
    assert report["delivery_mode"] == "single_my_email_blocked"
