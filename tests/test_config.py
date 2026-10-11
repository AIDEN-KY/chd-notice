"""配置边界及敏感值保护契约，不联网。"""

from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import healthcheck
from config import Settings


@pytest.fixture
def local_health_configuration(tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"
    auth.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    configuration = SimpleNamespace(
        ai_summary_enabled=False,
        multi_user_delivery_ready=False,
        auth_file=auth,
        database_file=tmp_path / "notices.db",
        attachment_dir=tmp_path / "downloads",
        log_dir=tmp_path / "logs",
        timezone=ZoneInfo("Asia/Shanghai"),
    )
    monkeypatch.setattr(healthcheck, "settings", configuration)
    for name in ("QQ_EMAIL", "MY_EMAIL"):
        monkeypatch.setenv(name, "valid@example.test")
    monkeypatch.setenv("QQ_SMTP_AUTH_CODE", "fake-authorization-secret")
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    return configuration


@pytest.mark.parametrize("name", ["QQ_EMAIL", "MY_EMAIL"])
def test_doctor_rejects_invalid_mailbox_without_echoing_it(
    local_health_configuration, monkeypatch, caplog, name
):
    value = "PRIVATE_EMAIL_MARKER\r\nBcc: unwanted@example.test"
    monkeypatch.setenv(name, value)
    assert healthcheck.run_healthcheck() is False
    assert "PRIVATE_EMAIL_MARKER" not in caplog.text
    assert "unwanted@example.test" not in caplog.text
    assert "fake-authorization-secret" not in caplog.text


@pytest.mark.parametrize(
    "field,value",
    [
        ("schedule_hour", 25),
        ("schedule_minute", 60),
        ("smtp_port", 70000),
        ("retry_attempts", 0),
    ],
)
def test_settings_reject_out_of_range_runtime_values(field, value):
    with pytest.raises(ValueError):
        Settings(**{field: value})


def test_summary_concurrency_above_hard_limit_is_rejected(monkeypatch):
    monkeypatch.setenv("SUMMARY_CONCURRENCY", "999")
    with pytest.raises(ValueError) as exc:
        Settings()
    assert "999" not in str(exc.value)
