"""摘要缓存契约：仅使用假 HTTP 和临时数据库。"""

from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import db
import summarizer
from tests.test_service import _run_immediate


@pytest.mark.parametrize("changed", [None, "deepseek_model", "summary_prompt_version", "content"])
def test_identical_summary_requests_share_persistent_cache(tmp_path, monkeypatch, changed):
    configuration = SimpleNamespace(
        database_file=tmp_path / "notices.db",
        timezone=ZoneInfo("Asia/Shanghai"),
        ai_summary_enabled=True,
        ai_excluded_keywords=("禁止转发",),
        deepseek_url="https://api.example.test/chat/completions",
        deepseek_api_key="fake-key",
        deepseek_model="fake-model",
        summary_prompt_version="v1",
        retry_attempts=1,
        retry_backoff_seconds=0,
    )
    monkeypatch.setattr(db, "settings", configuration)
    monkeypatch.setattr(summarizer, "settings", configuration)
    db.init_db()
    requests = []
    expected = (
        "学校将于十月十二日十九点在图书馆报告厅举办学习交流活动，请同学提前报名，"
        "携带学生证准时参加，活动结束后可向现场工作人员咨询后续安排。"
    )

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": expected}}]}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, url, **kwargs):
            requests.append(kwargs["json"])
            return FakeResponse()

    monkeypatch.setattr(summarizer.httpx, "AsyncClient", FakeClient)
    content = f"<p>{expected}</p>"
    first = _run_immediate(summarizer.summarize("学习交流通知", content))
    if changed == "content":
        content += "<p>补充说明</p>"
    elif changed:
        setattr(configuration, changed, "v2")
    second = _run_immediate(summarizer.summarize("学习交流通知", content))
    assert first == second == expected
    assert len(requests) == (2 if changed else 1)
