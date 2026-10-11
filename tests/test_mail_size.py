"""邮件大小与链接契约；仅构造 MIME 并捕获，不连接 SMTP。"""

from email.policy import SMTP
from types import SimpleNamespace

import pytest

import mailer


@pytest.fixture
def captured_mail(tmp_path, monkeypatch):
    configuration = SimpleNamespace(
        qq_email="sender@example.test",
        my_email="admin@example.test",
        unsubscribe_url="",
        max_attachment_bytes=20 * 1024 * 1024,
        max_message_bytes=100_000,
    )
    sent = []
    monkeypatch.setattr(mailer, "settings", configuration)
    monkeypatch.setattr(mailer, "_send_message", lambda message: sent.append(message))
    return configuration, sent


def _notice(path):
    return {
        "title": "Notice",
        "summary": "Summary",
        "detail_url": "https://example.test/notice",
        "attachments": [
            {"name": "report.bin", "path": str(path), "url": "https://example.test/download/report"}
        ],
    }


def test_encoded_message_budget_falls_back_to_links(captured_mail, tmp_path):
    settings, sent = captured_mail
    path = tmp_path / "report.bin"
    path.write_bytes(b"x" * 80_000)
    assert path.stat().st_size < settings.max_attachment_bytes
    mailer.send_digest([_notice(path)], "2026-10-11", "<test@example.test>")
    assert len(sent) == 1
    assert len(sent[0].as_bytes(policy=SMTP)) <= settings.max_message_bytes
    assert list(sent[0].iter_attachments()) == []
    assert (
        "https://example.test/download/report"
        in sent[0].get_body(preferencelist=("plain",)).get_content()
    )


def test_links_only_message_exceeding_budget_never_calls_smtp(captured_mail, tmp_path):
    settings, sent = captured_mail
    settings.max_message_bytes = 100
    with pytest.raises(ValueError, match="MIME"):
        mailer.send_digest([_notice(tmp_path / "absent")], "2026-10-11", "<test@example.test>")
    assert sent == []


@pytest.mark.parametrize("raw_limit,expected_attached", [(2, True), (1, False)])
def test_raw_attachment_limit_boundary(captured_mail, tmp_path, raw_limit, expected_attached):
    settings, sent = captured_mail
    settings.max_attachment_bytes = raw_limit
    path = tmp_path / "report.bin"
    path.write_bytes(b"xx")
    mailer.send_digest([_notice(path)], "2026-10-11", "<test@example.test>")
    assert bool(list(sent[0].iter_attachments())) is expected_attached


def test_invalid_mail_budget_configuration_does_not_echo_value(monkeypatch):
    from config import Settings

    monkeypatch.setenv("MAX_MESSAGE_BYTES", "secret-invalid-budget")
    with pytest.raises(ValueError) as exc:
        Settings()
    assert "secret-invalid-budget" not in str(exc.value)


def test_plain_text_includes_link_when_raw_attachment_budget_exceeded(captured_mail, tmp_path):
    settings, sent = captured_mail
    settings.max_attachment_bytes = 1
    path = tmp_path / "report.bin"
    path.write_bytes(b"xx")
    mailer.send_digest([_notice(path)], "2026-10-11", "<test@example.test>")
    assert list(sent[0].iter_attachments()) == []
    assert (
        "https://example.test/download/report"
        in sent[0].get_body(preferencelist=("plain",)).get_content()
    )
