"""摘要外发策略契约：全部使用假 HTTP，不发送真实校内通知。"""

from types import SimpleNamespace

import pytest

import summarizer
from tests.test_service import _run_immediate


def test_sensitive_notice_and_attachments_never_enter_recipient_delivery(tmp_path, monkeypatch):
    """整条分发保护必须独立于 AI 降级，不能向 SMTP 传入敏感通知。"""
    import db
    import service
    from tests.test_service import _configure_temp_database

    _configure_temp_database(tmp_path, monkeypatch)
    # 仅测试替身开启投递入口，正式配置保持关闭。
    service.settings.ai_excluded_keywords = ("禁止转发",)
    db.add_user("consenting-student@example.test", consent_source="offline-test-consent")
    db.save_notice(
        {
            "title": "测试通知",
            "publish_time": "2026-10-11",
            "department": "测试部门",
            "detail_url": "https://example.test/restricted-notice",
            "content_html": "<p>禁止转发：仅供内部查阅。</p>",
            "attachments": [{"name": "内部附件.pdf", "url": "https://example.test/restricted.pdf"}],
        },
        "restricted-content-hash",
    )
    summary_calls = []
    smtp_calls = []

    async def fake_summarize(title, content_html):
        summary_calls.append(title)
        return title

    def fake_send_digest(notices, *_args, **kwargs):
        smtp_calls.append(notices)
        return {"accepted": kwargs["recipients"], "refused": {}}

    async def fake_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(service, "summarize", fake_summarize)
    monkeypatch.setattr(service, "send_digest", fake_send_digest)
    monkeypatch.setattr(service.asyncio, "to_thread", fake_to_thread)

    delivered = _run_immediate(service.send_pending_today())
    assert smtp_calls == [], "禁止转发的通知及附件仍进入了 SMTP 投递"
    assert summary_calls == [], "敏感通知不应进入摘要处理"
    assert delivered == 0
    assert len(db.get_unsent_notices()) == 1, "被排除的通知不应误标为已发送"


@pytest.mark.parametrize("location", ["title", "body", "attachment"])
def test_distribution_policy_checks_full_text_and_attachment_names(location):
    from content_policy import notice_is_excluded

    notice = {"title": "允许通知", "content_html": "<p>正常正文</p>", "attachments": []}
    assert not notice_is_excluded(notice, ("禁止转发",))
    if location == "title":
        notice["title"] = "禁止转发的通知"
    elif location == "body":
        notice["content_html"] = "<p>" + "正文" * 10000 + "禁止<b>转发</b></p>"
    else:
        notice["attachments"] = [{"name": "禁止转发.pdf"}]
    assert notice_is_excluded(notice, ("禁止转发",))


@pytest.mark.parametrize("scenario", ["new", "frozen_sensitive", "latest_sensitive"])
def test_mixed_distribution_filters_new_batches_but_preserves_frozen_batches(
    tmp_path, monkeypatch, scenario
):
    import db
    import service
    from tests.test_service import _configure_temp_database

    _configure_temp_database(tmp_path, monkeypatch)
    frozen = scenario != "new"
    service.settings.ai_excluded_keywords = ("禁止转发",)
    service.settings.max_notices_per_digest = 1
    db.add_user("student@example.test", consent_source="test-consent")
    for index, body in enumerate(("禁止转发", "正常活动通知")):
        if scenario == "latest_sensitive":
            body = "原始允许正文"
        db.save_notice(
            {
                "title": f"测试{index}",
                "publish_time": "2026-10-11",
                "department": "测试部门",
                "detail_url": f"https://example.test/{index}",
                "content_html": f"<p>{body}</p>",
                "attachments": [],
            },
            f"hash-{index}",
        )
    if frozen:
        service.settings.max_notices_per_digest = 20
        db.prepare_recipient_batches(db.get_active_users())
        with db.connect() as conn:
            conn.execute("UPDATE batch_recipients SET status='refused',attempts=1")
    before = db.get_batch_notices(1) if frozen else None
    if scenario == "latest_sensitive":
        latest = db.get_notice_by_url("https://example.test/0")
        latest["content_html"] = "<p>修订后禁止转发</p>"
        db.save_notice(latest, "new-restricted-hash")
    sent = []

    async def summary(title, _body):
        return title

    def smtp(notices, *_args, **kwargs):
        sent.append(notices)
        return {"accepted": kwargs["recipients"], "refused": {}}

    async def to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(service, "summarize", summary)
    monkeypatch.setattr(service, "send_digest", smtp)
    monkeypatch.setattr(service.asyncio, "to_thread", to_thread)
    delivered = _run_immediate(service.send_pending_today())
    if frozen:
        assert delivered == 0
        assert sent == []
        assert db.get_batch_notices(1) == before
        with db.connect() as conn:
            recipient = conn.execute("SELECT * FROM batch_recipients").fetchone()
            assert recipient["status"] == "refused"
            assert recipient["attempts"] == 1
            assert conn.execute("SELECT COUNT(*) FROM daily_digest_reservations").fetchone()[0] == 0
    else:
        assert delivered == 1
        assert [n["title"] for n in sent[0]] == ["测试1"]
        assert [n["title"] for n in db.get_unsent_notices()] == ["测试0"]


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
