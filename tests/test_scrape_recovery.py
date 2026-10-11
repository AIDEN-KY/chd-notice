"""详情失败隔离契约：假 Playwright / 列表 / 详情，真实临时 SQLite。"""

from types import SimpleNamespace

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

import db
import scraper
from tests.test_service import _configure_temp_database, _run_immediate


@pytest.fixture
def fake_scrape(tmp_path, monkeypatch):
    _configure_temp_database(tmp_path, monkeypatch)
    auth = tmp_path / "auth.json"
    auth.touch()
    configuration = SimpleNamespace(
        auth_file=auth,
        max_pages_to_scrape=2,
        known_pages_to_stop=1,
        list_page_size=12,
        retry_attempts=2,
        retry_backoff_seconds=0,
    )
    monkeypatch.setattr(scraper, "settings", configuration)
    notices = [
        {"title": f"Notice {i}", "detail_url": f"https://example.test/{i}"} for i in range(1, 10)
    ]
    state = {"calls": [], "fail": True, "closed": []}

    async def detail(_context, url):
        state["calls"].append(url)
        if url.endswith("/8") and state["fail"]:
            raise PlaywrightTimeoutError("simulated temporary detail timeout")
        return "<p>Valid notice</p>", []

    async def listing(_context, page_number, _existing):
        return (
            [dict(n) for n in notices if n["detail_url"] != state.get("omit")]
            if page_number == 1
            else []
        )

    async def noop(*_args, **_kwargs):
        pass

    async def new_page():
        return SimpleNamespace(url="https://example.test/list")

    async def close_context():
        state["closed"].append("context")

    async def close_browser():
        state["closed"].append("browser")

    context = SimpleNamespace(new_page=new_page, close=close_context)

    async def new_context(**_kwargs):
        return context

    async def launch(**_kwargs):
        return SimpleNamespace(new_context=new_context, close=close_browser)

    class Playwright:
        async def __aenter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(launch=launch))

        async def __aexit__(self, *_args):
            pass

    monkeypatch.setattr(scraper, "async_playwright", Playwright)
    monkeypatch.setattr(scraper, "_open_notice_list", noop)
    monkeypatch.setattr(scraper, "_fetch_api_list", listing)
    monkeypatch.setattr(scraper, "_scrape_detail", detail)
    monkeypatch.setattr(scraper, "_save_auth_state_atomic", noop)
    monkeypatch.setattr(scraper.asyncio, "sleep", noop)
    return state


def test_eighth_detail_timeout_does_not_prevent_ninth_notice(fake_scrape):
    stats = _run_immediate(scraper.scrape_notices())
    assert "https://example.test/9" in fake_scrape["calls"]
    assert stats.new == 8
    assert stats.failed == 1
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM notices WHERE detail_url='https://example.test/9'"
            ).fetchone()[0]
            == 1
        )
    assert fake_scrape["closed"] == ["context", "browser"]


def test_temporary_failure_is_persisted_and_compensated_next_run(fake_scrape):
    _run_immediate(scraper.scrape_notices())
    failures = db.get_pending_scrape_failures()
    assert [row["detail_url"] for row in failures] == ["https://example.test/8"]
    fake_scrape["fail"] = False
    fake_scrape["omit"] = "https://example.test/8"
    _run_immediate(scraper.scrape_notices())
    assert db.get_pending_scrape_failures() == []
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM notices WHERE detail_url='https://example.test/8'"
            ).fetchone()[0]
            == 1
        )


def test_attachment_same_url_content_change_affects_notice_hash():
    notice = {
        "title": "Notice",
        "content_html": "<p>Content</p>",
        "attachments": [
            {"name": "report.pdf", "url": "https://example.test/report", "sha256": "old-hash"},
        ],
    }
    original = scraper.notice_content_hash(notice)
    notice["attachments"][0]["sha256"] = "new-hash"
    assert scraper.notice_content_hash(notice) != original


@pytest.mark.parametrize(
    "error", [scraper.SessionExpiredError("expired"), RuntimeError("missing structure")]
)
def test_systemic_failure_stops_instead_of_skipping(fake_scrape, monkeypatch, error):
    async def detail(_context, url):
        fake_scrape["calls"].append(url)
        raise error

    monkeypatch.setattr(scraper, "_scrape_detail", detail)
    with pytest.raises(type(error)):
        _run_immediate(scraper.scrape_notices())
    assert len(fake_scrape["calls"]) == 1
    assert db.get_pending_scrape_failures() == []
    with db.connect() as conn:
        run = conn.execute("SELECT * FROM scrape_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert run["status"] == "failed"
    assert run["finished_at"]
    assert fake_scrape["closed"] == ["context", "browser"]


@pytest.mark.parametrize("scenario", ["same-content", "new-content", "download-failed"])
def test_attachment_recheck_migration_and_failure_preserve_delivery_facts(
    fake_scrape, monkeypatch, scenario
):
    notice = {
        "title": "Attachment notice",
        "detail_url": "https://example.test/attachment",
        "content_html": "<p>Body</p>",
        "attachments": [
            {
                "name": "report",
                "url": "https://example.test/file",
                "path": "old-path",
                "sha256": "old-sha",
            }
        ],
    }
    old_hash = scraper.notice_content_hash(
        notice, include_attachment_hash=scenario != "same-content"
    )
    db.save_notice(notice, old_hash)
    with db.connect() as conn:
        conn.execute("UPDATE notices SET sent=1 WHERE detail_url=?", (notice["detail_url"],))
    original = db.get_notice_by_url(notice["detail_url"])

    async def detail(*_args):
        return notice["content_html"], [{"name": "report", "url": "https://example.test/file"}]

    async def download(_context, url, name):
        return {
            "name": name,
            "url": url,
            "path": "" if scenario == "download-failed" else "new-path",
            "sha256": "new-sha" if scenario == "new-content" else "old-sha",
        }

    monkeypatch.setattr(scraper, "_scrape_detail", detail)
    monkeypatch.setattr(scraper, "_download_attachment", download)
    stats = scraper.ScrapeStats()
    _run_immediate(scraper._collect_notice(None, notice, stats))
    current = db.get_notice_by_url(notice["detail_url"])
    if scenario == "same-content":
        assert current["sent"] == 1
        assert current["revision_count"] == 1
        assert stats.baselined == 1
    elif scenario == "new-content":
        assert current["sent"] == 0
        assert current["revision_count"] == 2
        assert stats.updated == 1
    else:
        assert current == original
        assert stats.failed == 1
        assert db.get_pending_scrape_failures()[0]["detail_url"] == notice["detail_url"]
