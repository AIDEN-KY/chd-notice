from __future__ import annotations

from scraper import ScrapeStats, evaluate_pagination_page, notice_content_hash
from service import delivery_identity
from version import VERSION


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
