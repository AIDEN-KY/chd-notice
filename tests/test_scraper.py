from __future__ import annotations

from types import SimpleNamespace

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

    monkeypatch.setattr(scraper, "settings", SimpleNamespace(
        auth_file=auth_file,
        notices_url=page_url,
        spa_wait_ms=1,
        list_item_selectors=[".missing-notice-row"],
    ))
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
