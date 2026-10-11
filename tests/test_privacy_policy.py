"""摘要外发策略契约：全部使用假 HTTP，不发送真实校内通知。"""

from types import SimpleNamespace

import pytest

import summarizer
from tests.test_service import _run_immediate


@pytest.fixture
def summary_configuration(monkeypatch):
    configuration = SimpleNamespace(
        ai_summary_enabled=True,
        ai_excluded_keywords=("内部资料", "禁止转发"),
        deepseek_url="https://api.example.test/chat/completions",
        deepseek_api_key="fake-key",
        deepseek_model="fake-model",
        retry_attempts=1,
        retry_backoff_seconds=0,
    )
    monkeypatch.setattr(summarizer, "settings", configuration)
    return configuration


def test_ai_disabled_never_constructs_http_client(summary_configuration, monkeypatch):
    summary_configuration.ai_summary_enabled = False

    def forbidden_client(*_args, **_kwargs):
        pytest.fail("关闭 AI 后不应构造 HTTP 客户端或外发正文")

    monkeypatch.setattr(summarizer.httpx, "AsyncClient", forbidden_client)
    assert _run_immediate(summarizer.summarize("活动通知", "<p>活动地点：报告厅</p>")) == "活动通知"


def test_sensitive_notice_is_not_sent_to_ai(summary_configuration, monkeypatch):
    def forbidden_client(*_args, **_kwargs):
        pytest.fail("命中敏感标记的正文不应提交给 DeepSeek")

    monkeypatch.setattr(summarizer.httpx, "AsyncClient", forbidden_client)
    assert (
        _run_immediate(summarizer.summarize("内部资料通知", "<p>禁止转发的人员名单</p>"))
        == "内部资料通知"
    )


def test_sensitive_marker_after_prompt_limit_is_still_checked(summary_configuration, monkeypatch):
    def forbidden_client(*_args, **_kwargs):
        pytest.fail("正文截断之前必须检查全文敏感标记")

    monkeypatch.setattr(summarizer.httpx, "AsyncClient", forbidden_client)
    assert (
        _run_immediate(summarizer.summarize("通知", "<p>" + "普通内容" * 4000 + "禁止转发</p>"))
        == "通知"
    )


@pytest.mark.parametrize("value", ["true", "1", "false", "0"])
def test_ai_switch_parses_explicit_boolean_values(monkeypatch, value):
    from config import Settings

    monkeypatch.setenv("AI_SUMMARY_ENABLED", value)
    assert Settings().ai_summary_enabled is (value in {"true", "1"})


def test_invalid_ai_configuration_fails_without_echoing_value(monkeypatch):
    from config import Settings

    monkeypatch.setenv("AI_SUMMARY_ENABLED", "secret-invalid-value")
    with pytest.raises(ValueError) as exc:
        _ = Settings().ai_summary_enabled
    assert "secret-invalid-value" not in str(exc.value)
    monkeypatch.setenv("AI_EXCLUDED_KEYWORDS", " || ")
    with pytest.raises(ValueError):
        _ = Settings().ai_excluded_keywords


def test_doctor_allows_disabled_ai_without_api_key(tmp_path, monkeypatch, caplog):
    import healthcheck

    auth_file = tmp_path / "auth.json"
    auth_file.touch()
    monkeypatch.setattr(
        healthcheck,
        "settings",
        SimpleNamespace(
            ai_summary_enabled=False,
            auth_file=auth_file,
            attachment_dir=tmp_path / "attachments",
            log_dir=tmp_path / "logs",
            database_file=tmp_path / "notices.db",
            multi_user_delivery_ready=False,
        ),
    )
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    for name in ("QQ_EMAIL", "QQ_SMTP_AUTH_CODE", "MY_EMAIL"):
        monkeypatch.setenv(
            name,
            "fake-sensitive-value"
            if name == "QQ_SMTP_AUTH_CODE"
            else "fake-sensitive-value@example.test",
        )
    assert healthcheck.run_healthcheck()
    assert "fake-sensitive-value" not in caplog.text


def test_summary_prompt_prioritizes_actionable_details(summary_configuration, monkeypatch):
    requests = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "假摘要，不代表真实模型验收"}}]}

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, _url, **kwargs):
            requests.append(kwargs["json"])
            return Response()

    monkeypatch.setattr(summarizer.httpx, "AsyncClient", Client)
    _run_immediate(
        summarizer.summarize("报名通知", "<p>10月12日18:00，报告厅，QQ群123456，联系人张老师。</p>")
    )
    assert len(requests) == 1
    prompt = requests[0]["messages"][-1]["content"]
    for requirement in ("时间", "地点", "QQ群", "联系方式", "不得编造"):
        assert requirement in prompt
