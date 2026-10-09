from __future__ import annotations

from types import SimpleNamespace
from zoneinfo import ZoneInfo

import mailer


def test_system_alert_builds_a_separate_failure_email(monkeypatch) -> None:
    sent = []
    monkeypatch.setattr(
        mailer,
        "settings",
        SimpleNamespace(
            qq_email="sender@example.test",
            my_email="receiver@example.test",
            timezone=ZoneInfo("Asia/Shanghai"),
        ),
    )
    monkeypatch.setattr(mailer, "_send_message", sent.append)

    mailer.send_system_alert("CAS 会话已过期", "请重新登录")

    assert len(sent) == 1
    message = sent[0]
    assert message["To"] == "receiver@example.test"
    assert "系统故障" in str(message["Subject"])
    assert "请重新登录" in message.get_body(preferencelist=("plain",)).get_content()
