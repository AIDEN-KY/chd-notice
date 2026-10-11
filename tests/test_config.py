"""配置边界及敏感值保护契约，不联网。"""

from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import healthcheck
from config import Settings


@pytest.mark.parametrize(
    "url", ["", "https://example.test/unsubscribe", "https://example.test:8443/u"]
)
def test_unsubscribe_url_allows_empty_or_https(monkeypatch, url):
    monkeypatch.setenv("UNSUBSCRIBE_URL", url)
    assert Settings().unsubscribe_url == url


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert('PRIVATE_URL_MARKER')",
        "http://example.test/PRIVATE_URL_MARKER",
        "https://user:PRIVATE_URL_MARKER@example.test/unsubscribe",
        "https://example.test/PRIVATE_URL_MARKER\nheader",
        "https://example.test:invalid/PRIVATE_URL_MARKER",
        "//example.test/PRIVATE_URL_MARKER",
    ],
)
def test_unsubscribe_url_rejects_unsafe_configuration_without_echo(monkeypatch, url):
    monkeypatch.setenv("UNSUBSCRIBE_URL", url)
    with pytest.raises(ValueError) as exc:
        _ = Settings().unsubscribe_url
    assert "UNSUBSCRIBE_URL" in str(exc.value)
    assert "PRIVATE_URL_MARKER" not in str(exc.value)


def test_doctor_rejects_unsafe_unsubscribe_url(local_health_configuration, monkeypatch, caplog):
    configuration = Settings(
        **{
            name: getattr(local_health_configuration, name)
            for name in ("auth_file", "database_file", "attachment_dir", "log_dir")
        }
    )
    monkeypatch.setattr(healthcheck, "settings", configuration)
    monkeypatch.setenv("AI_SUMMARY_ENABLED", "false")
    monkeypatch.setenv("UNSUBSCRIBE_URL", "javascript:alert('PRIVATE_URL_MARKER')")
    assert healthcheck.run_healthcheck() is False
    assert "PRIVATE_URL_MARKER" not in caplog.text


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
