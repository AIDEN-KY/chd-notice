"""正文资源变化契约：文字相同不代表通知内容未修改。"""

from types import SimpleNamespace

import pytest

import db
import scraper
from scraper import notice_content_hash
from tests.test_service import _configure_temp_database, _run_immediate


def _notice(content):
    return {
        "title": "报名通知",
        "detail_url": "https://portal.chd.edu.cn/notices/1",
        "content_html": content,
        "attachments": [],
    }


@pytest.mark.parametrize(
    "before,after",
    [
        (
            '<p><a href="https://example.test/old">报名入口</a></p>',
            '<p><a href="https://example.test/new">报名入口</a></p>',
        ),
        (
            '<p>活动海报</p><img src="/images/old.png">',
            '<p>活动海报</p><img src="/images/new.png">',
        ),
        (
            '<p>活动海报</p><img src="/images/base.png" srcset="/images/old.png 2x">',
            '<p>活动海报</p><img src="/images/base.png" srcset="/images/new.png 2x">',
        ),
    ],
    ids=["link-target", "image-source", "responsive-image-source"],
)
def test_resource_target_change_creates_different_notice_hash(before, after):
    assert notice_content_hash(_notice(before)) != notice_content_hash(_notice(after))


def test_equivalent_relative_target_and_style_changes_do_not_create_update():
    before = '<a href="/forms/signup" style="color:red">报名</a><img src="/images/poster.png" width="200">'
    after = '<a class="new-style" href="https://portal.chd.edu.cn/forms/signup">报名</a><img src="https://portal.chd.edu.cn/images/poster.png" width="300">'
    assert notice_content_hash(_notice(before)) == notice_content_hash(_notice(after))


@pytest.mark.parametrize("changed", [False, True])
def test_legacy_resource_hash_migration_preserves_sent_unless_target_changed(
    tmp_path, monkeypatch, changed
):
    _configure_temp_database(tmp_path, monkeypatch)
    monkeypatch.setattr(scraper, "settings", SimpleNamespace(retry_attempts=1))
    notice = _notice('<p><a href="/forms/old">报名入口</a></p>')
    db.save_notice(notice, notice_content_hash(notice, include_body_resources=False))
    with db.connect() as conn:
        conn.execute("UPDATE notices SET sent=1")

    async def detail(*_args):
        return (
            '<p><a href="/forms/new">报名入口</a></p>' if changed else notice["content_html"]
        ), []

    monkeypatch.setattr(scraper, "_scrape_detail", detail)
    stats = scraper.ScrapeStats()
    _run_immediate(scraper._collect_notice(None, notice, stats))
    current = db.get_notice_by_url(notice["detail_url"])
    assert current["sent"] == (0 if changed else 1)
    assert current["revision_count"] == (2 if changed else 1)
    assert stats.updated == int(changed)
    assert stats.baselined == int(not changed)
    # 同一内容下一次检查不应再次生成修订。
    second = scraper.ScrapeStats()
    _run_immediate(scraper._collect_notice(None, notice, second))
    assert second.unchanged == 1
