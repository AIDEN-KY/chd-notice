from __future__ import annotations

from types import SimpleNamespace

import pytest

import mailer


def test_digest_uses_explicit_bcc_envelope_without_leaking_twenty_addresses(monkeypatch):
    recipients = [f"student{index}@example.test" for index in range(20)]
    captured = []

    class FakeSMTP:
        def __init__(self, host, port, *, timeout):
            assert (host, port, timeout) == ("smtp.example.test", 465, 30)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def login(self, username, password):
            assert (username, password) == ("sender@example.test", "test-only-code")

        def send_message(self, message, from_addr=None, to_addrs=None):
            captured.append((message, to_addrs))
            return {}

    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(
        mailer,
        "settings",
        SimpleNamespace(
            smtp_host="smtp.example.test",
            smtp_port=465,
            qq_email="sender@example.test",
            qq_smtp_auth_code="test-only-code",
            my_email="administrator@example.test",
            unsubscribe_url="",
            max_attachment_bytes=20 * 1024 * 1024,
        ),
    )
    # Proposed explicit recipients API; no real SMTP, credentials, or network are used.
    mailer.send_digest(
        [
            {
                "title": "Notice",
                "summary": "Summary",
                "detail_url": "https://example.test/notice",
                "attachments": [],
            }
        ],
        "2026-10-11",
        "<bcc-test@example.test>",
        recipients=recipients,
    )
    assert len(captured) == 1
    message, envelope = captured[0]
    assert list(envelope) == recipients
    assert str(message["To"]).lower() == "undisclosed-recipients:;"
    assert message["Cc"] is None
    assert message["Bcc"] is None
    raw_message = message.as_bytes()
    for address in recipients:
        assert address.encode("ascii") not in raw_message
    assert b"administrator@example.test" not in raw_message


@pytest.mark.parametrize(
    "recipients", [[], ["a\r\nBcc:b@example.test"], [f"u{i}@example.test" for i in range(21)]]
)
def test_bcc_rejects_empty_invalid_or_oversized_recipient_set(monkeypatch, recipients):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("invalid envelope must not reach SMTP")

    monkeypatch.setattr(mailer, "_send_message", forbidden)
    with pytest.raises(ValueError):
        mailer.send_digest([], "2026-10-11", "<test@example.test>", recipients=recipients)
