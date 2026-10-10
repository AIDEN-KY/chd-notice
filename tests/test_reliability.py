from __future__ import annotations

from types import SimpleNamespace
from typing import Coroutine, TypeVar

import scraper
from scraper import ScrapeStats, evaluate_pagination_page, notice_content_hash
from service import delivery_identity
from version import VERSION

T = TypeVar("T")


def _run_immediate(coroutine: Coroutine[object, object, T]) -> T:
    """执行完全由测试替身满足、不会真正挂起的协程。"""
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    raise AssertionError("测试协程意外进入挂起状态")


def test_notice_hash_ignores_html_formatting_and_attachment_order() -> None:
    first = {
        "title": " 通知 ",
        "publish_time": "2026-10-10",
        "department": "部门",
        "content_html": "<p>请 <strong>及时</strong> 报名</p>",
        "attachments": [
            {"name": "B.docx", "url": "https://example.test/b"},
            {"name": "A.pdf", "url": "https://example.test/a"},
        ],
    }
    second = {
        **first,
        "title": "通知",
        "content_html": "<div>请 及时 报名</div>",
        "attachments": list(reversed(first["attachments"])),
    }

    assert notice_content_hash(first) == notice_content_hash(second)
    second["content_html"] = "<div>报名时间已经变更</div>"
    assert notice_content_hash(first) != notice_content_hash(second)


def test_delivery_identity_changes_with_notice_revision() -> None:
    notices = [{"id": 7, "revision_count": 1, "content_hash": "one"}]
    key_v1, message_id_v1 = delivery_identity(notices)
    notices[0]["revision_count"] = 2
    notices[0]["content_hash"] = "two"
    key_v2, message_id_v2 = delivery_identity(notices)

    assert key_v1 != key_v2
    assert message_id_v1 != message_id_v2


def test_scrape_stats_changed_count_and_version() -> None:
    stats = ScrapeStats(new=2, updated=3, unchanged=4, baselined=5, pages=6)
    assert stats.changed == 5
    assert VERSION == "1.1.0"


def test_pagination_stops_after_three_consecutive_known_pages() -> None:
    seen_pages = set()
    consecutive = 0

    for page_number in range(1, 4):
        reason, consecutive = evaluate_pagination_page(
            (f"url-{page_number}",), seen_pages, consecutive, True, 3
        )
        assert reason == ("known-pages" if page_number == 3 else None)

    reason, consecutive = evaluate_pagination_page(
        ("new-url",), seen_pages, consecutive, False, 3
    )
    assert reason is None
    assert consecutive == 0


def test_pagination_stops_when_a_page_repeats() -> None:
    seen_pages = set()
    fingerprint = ("url-1", "url-2")
    reason, consecutive = evaluate_pagination_page(
        fingerprint, seen_pages, 0, False, 3
    )
    assert reason is None

    reason, consecutive = evaluate_pagination_page(
        fingerprint, seen_pages, consecutive, False, 3
    )
    assert reason == "repeated"


class _FakeLocator:
    def __init__(self, page: "_FakePage", selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> "_FakeLocator":
        return self

    def filter(self, **_kwargs: object) -> "_FakeLocator":
        return self

    async def count(self) -> int:
        if self.selector == ".row":
            return len(self.page.items)
        return int(self.page.mode == "click" and self.selector == ".next")

    async def is_visible(self) -> bool:
        return True

    async def click(self) -> None:
        self.page.items = ["second-page"]

    async def all_inner_texts(self) -> list[str]:
        return list(self.page.items)

    async def evaluate(self, _script: str, _argument: object) -> list[dict]:
        assert _argument == ["#app"]
        self.page.items = [*self.page.items, "loaded-by-scroll"]
        return [{"tag": "DIV", "id": "list", "className": "scrollable"}]


class _FakePage:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.items = ["first-page"]

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self, selector)

    async def wait_for_timeout(self, _milliseconds: int) -> None:
        return None

    async def wait_for_function(
        self, _script: str, *, arg: object, timeout: int
    ) -> None:
        assert timeout == 1234
        if arg == [".row"]:
            return
        assert arg == {"selector": ".row", "oldItems": ["first-page"]}
        assert self.items != ["first-page"]


def _pagination_settings() -> SimpleNamespace:
    return SimpleNamespace(
        list_item_selectors=[".row"],
        next_page_selectors=[".next"],
        spa_wait_ms=1,
        action_timeout_ms=1234,
        scroll_container_selectors=["#app"],
    )


def test_dynamic_pagination_clicks_next_control(monkeypatch) -> None:
    monkeypatch.setattr(scraper, "settings", _pagination_settings())
    page = _FakePage("click")

    assert _run_immediate(scraper._go_next(page, 2)) is True
    assert page.items == ["second-page"]


def test_list_wait_uses_playwright_keyword_argument(monkeypatch) -> None:
    monkeypatch.setattr(scraper, "settings", _pagination_settings())
    page = _FakePage("scroll")

    assert _run_immediate(scraper._wait_for_list(page, timeout_ms=1234)) == ".row"


def test_list_item_is_relocated_by_exact_title_with_locators(monkeypatch) -> None:
    class FakeTextNode:
        def __init__(self, text: str) -> None:
            self.text = text

        @property
        def first(self) -> "FakeTextNode":
            return self

        async def count(self) -> int:
            return 1

        async def inner_text(self) -> str:
            return self.text

    class FakeRow:
        def __init__(self, title: str) -> None:
            self.title = title

        def locator(self, selector: str) -> FakeTextNode:
            assert selector == ".title"
            return FakeTextNode(self.title)

    class FakeRows:
        def __init__(self, rows: list[FakeRow]) -> None:
            self.rows = rows

        async def count(self) -> int:
            return len(self.rows)

        def nth(self, index: int) -> FakeRow:
            return self.rows[index]

    class FakeTitlePage:
        def __init__(self, rows: list[FakeRow]) -> None:
            self.rows = FakeRows(rows)

        def locator(self, selector: str) -> FakeRows:
            assert selector == ".row"
            return self.rows

    rows = [FakeRow("第一条通知"), FakeRow("  目标   通知  ")]
    page = FakeTitlePage(rows)
    monkeypatch.setattr(scraper, "settings", SimpleNamespace(title_selectors=[".title"]))

    matched = _run_immediate(scraper._list_item_by_title(page, ".row", "目标 通知"))
    assert matched is rows[1]


def test_row_title_falls_back_to_first_line_when_read_marker_changes(monkeypatch) -> None:
    class MissingNode:
        @property
        def first(self) -> "MissingNode":
            return self

        async def count(self) -> int:
            return 0

    class ReadRow:
        def locator(self, selector: str) -> MissingNode:
            assert selector == "#readClass"
            return MissingNode()

        async def inner_text(self) -> str:
            return "  已读通知标题  \n某部门 | · 2026-10-10"

    monkeypatch.setattr(
        scraper,
        "settings",
        SimpleNamespace(title_selectors=["#readClass"]),
    )

    assert _run_immediate(scraper._row_title(ReadRow())) == "已读通知标题"


def test_dynamic_pagination_falls_back_to_infinite_scroll(monkeypatch) -> None:
    monkeypatch.setattr(scraper, "settings", _pagination_settings())
    page = _FakePage("scroll")

    assert _run_immediate(scraper._go_next(page, 2)) is True
    assert page.items == ["first-page", "loaded-by-scroll"]


def test_returning_from_detail_restores_loaded_list_batch(monkeypatch) -> None:
    monkeypatch.setattr(scraper, "settings", _pagination_settings())
    monkeypatch.setattr(
        scraper.asyncio,
        "get_running_loop",
        lambda: SimpleNamespace(time=lambda: 0.0),
    )
    page = _FakePage("scroll")

    assert _run_immediate(scraper._restore_list_count(page, ".row", 2)) is True
    assert page.items == ["first-page", "loaded-by-scroll"]


def test_notice_list_reenters_portal_when_spa_stays_blank(monkeypatch) -> None:
    test_settings = SimpleNamespace(
        retry_attempts=3,
        retry_backoff_seconds=1,
        notices_url="https://example.test/notices",
        spa_wait_ms=1,
        spa_load_timeout_ms=2,
        action_timeout_ms=3,
    )
    attempts = []

    class FakeOpenPage:
        def __init__(self) -> None:
            self.blank_navigations = 0

        async def wait_for_timeout(self, _milliseconds: int) -> None:
            return None

        async def goto(self, url: str, **_kwargs: object) -> None:
            assert url == "about:blank"
            self.blank_navigations += 1

    async def fake_goto_with_retry(_page: object, url: str) -> None:
        assert url == test_settings.notices_url

    async def fake_is_login_page(_page: object) -> bool:
        return False

    async def fake_wait_for_list(_page: object, timeout_ms: int) -> str | None:
        assert timeout_ms == test_settings.spa_load_timeout_ms
        attempts.append(True)
        return ".row" if len(attempts) == 2 else None

    monkeypatch.setattr(scraper, "settings", test_settings)
    monkeypatch.setattr(scraper, "_goto_with_retry", fake_goto_with_retry)
    monkeypatch.setattr(scraper, "_is_login_page", fake_is_login_page)
    monkeypatch.setattr(scraper, "_wait_for_list", fake_wait_for_list)
    page = FakeOpenPage()

    assert _run_immediate(scraper._open_notice_list(page)) == ".row"
    assert len(attempts) == 2
    assert page.blank_navigations == 1
