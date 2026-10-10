from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

import scraper


def test_missing_list_selector_still_collects_dom_diagnostics(tmp_path, monkeypatch):
    """已登录页面存在真实 DOM，但配置的列表选择器不存在时仍完成诊断。"""
    auth_file = tmp_path / "auth.json"
    auth_file.touch()
    events = []
    page_url = "https://example.test/notices"
    candidate = {"tag": "BUTTON", "text": "下一页", "html": "<button>下一页</button>"}

    class MissingLocator:
        async def count(self):
            return 0

    class FakePage:
        url = page_url
        frames = []

        def locator(self, selector):
            assert selector == ".missing-notice-row"
            return MissingLocator()

        async def wait_for_timeout(self, _timeout):
            pass

        async def evaluate(self, _script, arg=None):
            if arg is None:
                events.append("scroll")
                return None
            events.append("collect-dom")
            return {
                **arg,
                "url": self.url,
                "bodyTextEnd": "页面通知内容 下一页",
                "candidates": [candidate],
                "scrollables": [{"tag": "DIV", "id": "notice-container"}],
                "likelyRows": [{"selector": "div.actual-row", "count": 12}],
            }

    page = FakePage()

    async def new_page():
        return page

    async def close_context():
        events.append("close-context")

    context = SimpleNamespace(new_page=new_page, close=close_context)

    async def new_context(**kwargs):
        assert kwargs["storage_state"] == auth_file
        return context

    async def close_browser():
        events.append("close-browser")

    browser = SimpleNamespace(new_context=new_context, close=close_browser)

    async def launch(**_kwargs):
        return browser

    class FakePlaywright:
        async def __aenter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(launch=launch))

        async def __aexit__(self, *_args):
            pass

    async def goto(_page, url):
        assert url == page_url

    async def is_login(_page):
        return False

    async def wait_for_list(_page, timeout_ms):
        assert timeout_ms == 45_000
        return None

    monkeypatch.setattr(
        scraper,
        "settings",
        SimpleNamespace(
            auth_file=auth_file,
            notices_url=page_url,
            spa_wait_ms=1,
            list_item_selectors=[".missing-notice-row"],
        ),
    )
    monkeypatch.setattr(scraper, "async_playwright", FakePlaywright)
    monkeypatch.setattr(scraper, "_goto_with_retry", goto)
    monkeypatch.setattr(scraper, "_is_login_page", is_login)
    monkeypatch.setattr(scraper, "_wait_for_list", wait_for_list)

    # 所有异步 I/O 均由即时返回的替身完成，不创建浏览器或网络事件循环。
    coroutine = scraper.diagnose_pagination()
    try:
        coroutine.send(None)
    except StopIteration as completed:
        diagnostics = completed.value
    else:
        coroutine.close()
        raise AssertionError("测试替身不应挂起协程")

    assert diagnostics["matchedListSelector"] is None
    assert diagnostics["beforeCount"] == diagnostics["afterCount"] == 0
    assert diagnostics["configuredListSelectors"] == [".missing-notice-row"]
    assert diagnostics["bodyTextEnd"] == "页面通知内容 下一页"
    assert diagnostics["candidates"] == [candidate]
    assert diagnostics["scrollables"][0]["id"] == "notice-container"
    assert diagnostics["likelyRows"][0]["selector"] == "div.actual-row"
    assert events == ["scroll", "collect-dom", "close-context", "close-browser"]


@pytest.mark.parametrize(
    ("response_url", "is_login_redirect"),
    [
        ("https://ids.chd.edu.cn/authserver/login?service=portal", True),
        ("https://portal.chd.edu.cn/authserver/login", True),
        ("https://portal.chd.edu.cn/files/report.html", False),
    ],
    ids=["cas-host", "login-path", "legitimate-html-attachment"],
)
def test_attachment_download_rejects_login_redirects(
    tmp_path, monkeypatch, response_url, is_login_redirect
):
    """HTTP 200 登录页不能充当附件，但普通 HTML 文件仍可下载。"""
    original_url = "https://portal.chd.edu.cn/files/report.html"
    payload = b"<!doctype html><html><body>HTML document</body></html>"
    body_reads = []
    download_dir = tmp_path / "downloads"

    class FakeResponse:
        ok = True
        status = 200
        url = response_url
        headers = {"content-type": "text/html; charset=utf-8"}

        async def body(self):
            body_reads.append(True)
            return payload

    async def get(url, *, timeout):
        assert url == original_url
        assert timeout == 1234
        return FakeResponse()

    monkeypatch.setattr(
        scraper,
        "settings",
        SimpleNamespace(
            action_timeout_ms=1234,
            attachment_dir=download_dir,
            login_url="https://ids.chd.edu.cn/authserver/login",
        ),
    )
    context = SimpleNamespace(request=SimpleNamespace(get=get))
    coroutine = scraper._download_attachment(context, original_url, "report.html")
    try:
        coroutine.send(None)
    except StopIteration as completed:
        attachment = completed.value
    else:
        coroutine.close()
        raise AssertionError("测试替身不应挂起协程")

    assert attachment["url"] == original_url
    assert attachment["name"] == "report.html"
    if is_login_redirect:
        assert attachment["path"] == "", "登录响应不能保存为可发送的附件"
        assert attachment["size"] == 0
        assert body_reads == []
        assert not download_dir.exists()
    else:
        assert attachment["size"] == len(payload)
        assert body_reads == [True]
        assert len(list(download_dir.iterdir())) == 1
        assert next(download_dir.iterdir()).read_bytes() == payload


@pytest.mark.parametrize("fail_publish", [False, True], ids=["updated-content", "disk-failure"])
def test_attachment_update_preserves_previous_file(tmp_path, monkeypatch, fail_publish):
    """同 URL 更新不能覆盖旧版本；发布失败必须清理临时文件并降级为链接。"""
    import os

    original_url = "https://portal.chd.edu.cn/files/report.pdf"
    payload = b"old attachment"
    download_dir = tmp_path / "downloads"

    class FakeResponse:
        ok = True
        url = original_url

        async def body(self):
            return payload

    async def get(_url, **_kwargs):
        return FakeResponse()

    monkeypatch.setattr(
        scraper,
        "settings",
        SimpleNamespace(
            action_timeout_ms=1234,
            attachment_dir=download_dir,
            login_url="https://ids.chd.edu.cn/authserver/login",
        ),
    )
    context = SimpleNamespace(request=SimpleNamespace(get=get))

    def download():
        coroutine = scraper._download_attachment(context, original_url, "report.pdf")
        try:
            coroutine.send(None)
        except StopIteration as completed:
            return completed.value
        finally:
            coroutine.close()
        raise AssertionError("测试替身不应挂起协程")

    first = download()
    old_path = next(download_dir.iterdir())
    payload = b"new attachment"
    if fail_publish:

        def fail_replace(*_args, **_kwargs):
            raise OSError("simulated disk failure")

        monkeypatch.setattr(os, "replace", fail_replace)

    second = download()
    assert old_path.read_bytes() == b"old attachment", "更新不能破坏已存通知的附件"
    assert first["sha256"] == hashlib.sha256(b"old attachment").hexdigest()
    if fail_publish:
        assert second["path"] == ""
        assert second["size"] == 0
        assert second["url"] == original_url
        assert list(download_dir.iterdir()) == [old_path]
    else:
        assert second["path"] != first["path"]
        assert second["sha256"] == hashlib.sha256(payload).hexdigest()
        assert len(list(download_dir.iterdir())) == 2
        repeated = download()
        assert repeated["path"] == second["path"]
        assert len(list(download_dir.iterdir())) == 2
