"""历史轮转契约：固定十页假列表，不访问学校门户。"""

import pytest

import db
import scraper
from tests.test_scrape_recovery import fake_scrape as _fake_scrape
from tests.test_service import _run_immediate

fake_scrape = _fake_scrape


@pytest.fixture
def history_site(fake_scrape, monkeypatch):
    configuration = scraper.settings
    configuration.max_pages_to_scrape = 2
    configuration.known_pages_to_stop = 1
    configuration.list_page_size = 1
    configuration.history_scan_enabled = True
    configuration.history_pages_per_run = 1
    state = {"visited": [], "changed": False}

    def notice(page_number):
        return {
            "title": f"Page {page_number}",
            "detail_url": f"https://example.test/history/{page_number}",
            "content_html": "<p>Original</p>",
            "attachments": [],
        }

    async def listing(_context, page_number, _existing):
        state["visited"].append(page_number)
        return [notice(page_number)] if page_number <= 10 else []

    async def detail(_context, url):
        page_number = int(url.rsplit("/", 1)[1])
        return (
            "<p>Updated deadline</p>"
            if state["changed"] and page_number == 10
            else "<p>Original</p>"
        ), []

    monkeypatch.setattr(scraper, "_fetch_api_list", listing)
    monkeypatch.setattr(scraper, "_scrape_detail", detail)
    state["notice"] = notice
    return state


def test_tenth_page_update_is_found_within_bounded_rotation(history_site):
    for page in range(1, 11):
        notice = history_site["notice"](page)
        db.save_notice(notice, scraper.notice_content_hash(notice))
    history_site["changed"] = True
    for _ in range(10):
        before = len(history_site["visited"])
        _run_immediate(scraper.scrape_notices())
        assert len(history_site["visited"]) - before <= 2, "每日列表请求必须遵守总页数预算"
    tenth = db.get_notice_by_url("https://example.test/history/10")
    assert tenth["content_html"] == "<p>Updated deadline</p>", (
        "不能永远只扫描最新已知页而漏掉旧通知修改"
    )
    assert tenth["revision_count"] == 2


def test_page_budget_exhaustion_resumes_history_on_next_run(history_site):
    _run_immediate(scraper.scrape_notices())
    assert len(history_site["visited"]) <= 2
    history_site["visited"].clear()
    # 新一次调用只依赖临时 SQLite 中的持久状态，不复用上次协程内变量。
    _run_immediate(scraper.scrape_notices())
    assert len(history_site["visited"]) <= 2
    assert any(page >= 3 for page in history_site["visited"]), (
        "达到预算后下次必须续扫，不能每次从第一页耗尽预算"
    )


def test_history_cursor_wraps_and_coverage_is_recorded(history_site):
    for _ in range(10):
        _run_immediate(scraper.scrape_notices())
    assert db.get_history_scan_cursor() == 2
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM scrape_run_pages WHERE phase='history' ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
    assert row["page_number"] == 11
    assert row["stop_reason"] == "empty"


def test_disabled_rotation_preserves_saved_cursor(history_site):
    run_id = db.start_scrape_run()
    db.record_scan_page(run_id, "history", 6, 1, "budget", next_page=7)
    scraper.settings.history_scan_enabled = False
    _run_immediate(scraper.scrape_notices())
    assert history_site["visited"] == [1, 2]
    assert db.get_history_scan_cursor() == 7


def test_system_failure_does_not_advance_cursor(history_site, monkeypatch):
    async def detail(_context, url):
        if url.endswith("/2"):
            raise scraper.SessionExpiredError("expired")
        return "<p>Original</p>", []

    monkeypatch.setattr(scraper, "_scrape_detail", detail)
    with pytest.raises(scraper.SessionExpiredError):
        _run_immediate(scraper.scrape_notices())
    assert db.get_history_scan_cursor() == 2
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM scrape_run_pages WHERE phase='history'").fetchone()
    assert row["stop_reason"] == "system-failure"


def test_reordered_duplicate_notice_is_only_checked_once(history_site, monkeypatch):
    calls = []

    async def listing(_context, page_number, _existing):
        history_site["visited"].append(page_number)
        return [history_site["notice"](1)]

    async def detail(_context, url):
        calls.append(url)
        return "<p>Original</p>", []

    monkeypatch.setattr(scraper, "_fetch_api_list", listing)
    monkeypatch.setattr(scraper, "_scrape_detail", detail)
    stats = _run_immediate(scraper.scrape_notices())
    assert calls == ["https://example.test/history/1"]
    assert stats.new == 1
    assert stats.pages == 2


def test_multi_page_rotation_keeps_one_boundary_page_overlap(history_site):
    scraper.settings.max_pages_to_scrape = 3
    scraper.settings.history_pages_per_run = 2
    _run_immediate(scraper.scrape_notices())
    assert history_site["visited"] == [1, 2, 3]
    assert db.get_history_scan_cursor() == 3
    history_site["visited"].clear()
    _run_immediate(scraper.scrape_notices())
    assert history_site["visited"] == [1, 3, 4]
    assert db.get_history_scan_cursor() == 4
