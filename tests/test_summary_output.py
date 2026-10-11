"""摘要输出质量与关键字段契约，假 API，不外发正文。"""

import asyncio
from zoneinfo import ZoneInfo

import httpx
import pytest

import db
import summarizer
from tests.test_privacy_policy import summary_configuration as _summary_configuration
from tests.test_service import _run_immediate

summary_configuration = _summary_configuration


def _install_response(monkeypatch, content, *, pause=False, statuses=None):
    requests = []
    remaining_statuses = list(statuses or [])

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": content}}]}

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, *_args, **_kwargs):
            requests.append(True)
            if pause:
                await asyncio.sleep(0)
            if remaining_statuses and remaining_statuses.pop(0) == 429:
                response = httpx.Response(
                    429, request=httpx.Request("POST", "https://api.example.test")
                )
                response.raise_for_status()
            return Response()

    monkeypatch.setattr(summarizer.httpx, "AsyncClient", Client)
    return requests


@pytest.mark.parametrize(
    "output,reason",
    [
        ("   ", "empty_output"),
        ("好的", "output_too_short"),
        ("普通内容" * 200, "output_too_long"),
        ("<script>unsafe-secret-text</script>", "unsafe_output"),
        (None, "invalid_output_type"),
    ],
    ids=["empty", "too-short", "too-long", "markup", "invalid-type"],
)
def test_invalid_summary_falls_back_and_logs_sanitized_reason(
    summary_configuration, monkeypatch, caplog, output, reason
):
    summary_configuration.summary_min_chars = 50
    summary_configuration.summary_max_chars = 400
    _install_response(monkeypatch, output)
    result = _run_immediate(summarizer.summarize("活动通知", "<p>学校组织学习交流活动。</p>"))
    assert result == "活动通知"
    assert reason in caplog.text, "降级必须记录可检索的原因，而不是静默返回异常摘要"
    assert "unsafe-secret-text" not in caplog.text
    assert "fake-key" not in caplog.text


def test_valid_summary_cannot_silently_drop_explicit_critical_fields(
    summary_configuration, monkeypatch
):
    _install_response(
        monkeypatch,
        "学校将举办学习交流活动，欢迎同学提前报名并做好准备，活动包含主题分享和现场交流，"
        "请关注报名要求，携带学生证准时参加，具体安排以学校通知为准。",
    )
    fields = [
        "2026年10月20日19:00",
        "北校区图书馆报告厅",
        "123456789",
        "王老师",
        "13800138000",
        "notice@example.test",
        "2026年10月18日",
    ]
    body = (
        "<p>时间：2026年10月20日19:00；地点：北校区图书馆报告厅；QQ群：123456789；"
        "联系人：王老师；电话：13800138000；邮箱：notice@example.test；报名截止：2026年10月18日。</p>"
    )
    result = _run_immediate(summarizer.summarize("活动通知", body))
    for field in fields:
        assert field in result, f"摘要缺少原文明确给出的关键字段：{field}"


def test_concurrent_identical_summaries_share_one_request(summary_configuration, monkeypatch):
    output = "学校将举办学习交流活动，欢迎同学提前报名并做好准备，活动包含主题分享和现场交流，请关注报名要求，携带学生证准时参加，具体安排以学校通知为准。"
    requests = _install_response(monkeypatch, output, pause=True)

    async def run():
        return await asyncio.gather(
            *[summarizer.summarize("活动通知", "<p>学校组织学习交流活动。</p>") for _ in range(5)]
        )

    assert asyncio.run(run()) == [output] * 5
    assert len(requests) == 1
    assert summarizer._inflight == {}


def test_rate_limit_retries_with_backoff_then_validates_output(summary_configuration, monkeypatch):
    summary_configuration.retry_attempts = 2
    summary_configuration.retry_backoff_seconds = 1
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(summarizer.asyncio, "sleep", sleep)
    output = "学校将举办学习交流活动，欢迎同学提前报名并做好准备，活动包含主题分享和现场交流，请关注报名要求，携带学生证准时参加，具体安排以学校通知为准。"
    requests = _install_response(monkeypatch, output, statuses=[429, 200])
    assert _run_immediate(summarizer.summarize("活动通知", "<p>交流活动。</p>")) == output
    assert len(requests) == 2
    assert delays == [1]


def test_invalid_output_is_not_cached(summary_configuration, monkeypatch, tmp_path):
    summary_configuration.database_file = tmp_path / "notices.db"
    summary_configuration.timezone = ZoneInfo("Asia/Shanghai")
    monkeypatch.setattr(db, "settings", summary_configuration)
    db.init_db()
    requests = _install_response(monkeypatch, "好的")
    for _ in range(2):
        assert _run_immediate(summarizer.summarize("通知", "<p>交流活动。</p>")) == "通知"
    assert len(requests) == 2
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM summary_cache").fetchone()[0] == 0
