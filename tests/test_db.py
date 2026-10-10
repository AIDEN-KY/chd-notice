from __future__ import annotations

from types import SimpleNamespace
from zoneinfo import ZoneInfo

import db


def _notice(url: str = "https://example.test/notice/1", body: str = "正文") -> dict:
    return {
        "title": "测试通知",
        "publish_time": "2026-10-10",
        "department": "测试部门",
        "detail_url": url,
        "content_html": f"<p>{body}</p>",
        "attachments": [],
    }


def _use_temp_database(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        db,
        "settings",
        SimpleNamespace(
            database_file=tmp_path / "notices.db",
            timezone=ZoneInfo("Asia/Shanghai"),
        ),
    )
    db.init_db()


def test_historical_unsent_notice_is_still_selected(tmp_path, monkeypatch) -> None:
    _use_temp_database(tmp_path, monkeypatch)
    assert db.save_notice(_notice(), "hash-v1") == "new"
    with db.connect() as conn:
        conn.execute("UPDATE notices SET fetched_date = '2020-01-01'")

    pending = db.get_unsent_notices()

    assert len(pending) == 1
    assert pending[0]["fetched_date"] == "2020-01-01"


def test_existing_portal_url_is_found_by_stable_dm(tmp_path, monkeypatch) -> None:
    _use_temp_database(tmp_path, monkeypatch)
    old_url = "https://portal.chd.edu.cn/index.do#/show?dm=ABC123&lm=旧部门"
    db.save_notice(_notice(url=old_url), "hash-v1")
    assert db.get_existing_notice_urls() == {"ABC123": old_url}


def test_delivery_attempts_errors_and_completion_are_recorded(tmp_path, monkeypatch) -> None:
    _use_temp_database(tmp_path, monkeypatch)
    db.save_notice(_notice(), "hash-v1")
    notice_id = db.get_unsent_notices()[0]["id"]

    delivery = db.ensure_delivery("key-1", "<message-1@test>", [notice_id])
    assert delivery["status"] == "pending"
    assert db.start_delivery("key-1") == 1
    db.fail_delivery("key-1", "SMTP timeout")
    failed = db.get_delivery("key-1")
    assert failed is not None
    assert failed["status"] == "failed"
    assert failed["attempts"] == 1
    assert failed["failure_count"] == 1
    assert failed["last_error"] == "SMTP timeout"

    assert db.start_delivery("key-1") == 2
    db.complete_delivery("key-1", [notice_id])
    completed = db.get_delivery("key-1")
    assert completed is not None
    assert completed["status"] == "sent"
    assert completed["attempts"] == 2
    assert completed["failure_count"] == 1
    assert db.get_unsent_notices() == []


def test_delivery_failure_counter_migrates_without_losing_records(tmp_path, monkeypatch) -> None:
    _use_temp_database(tmp_path, monkeypatch)
    db.ensure_delivery("migration", "<migration@test>", [1])
    with db.connect() as conn:
        conn.execute("ALTER TABLE mail_deliveries DROP COLUMN failure_count")
    db.init_db()
    db.init_db()
    assert db.get_delivery("migration")["failure_count"] == 0
    db.start_delivery("migration")
    db.fail_delivery("migration", "first failure")
    db.start_delivery("migration")
    db.fail_delivery("migration", "second failure")
    delivery = db.get_delivery("migration")
    assert delivery["failure_count"] == 2
    assert delivery["last_error"] == "second failure"


def test_content_update_requeues_a_sent_notice(tmp_path, monkeypatch) -> None:
    _use_temp_database(tmp_path, monkeypatch)
    db.save_notice(_notice(), "hash-v1")
    notice_id = db.get_unsent_notices()[0]["id"]
    db.ensure_delivery("key-1", "<message-1@test>", [notice_id])
    db.complete_delivery("key-1", [notice_id])

    changed = _notice(body="正文已修改")
    assert db.save_notice(changed, "hash-v2") == "updated"

    pending = db.get_unsent_notices()
    assert len(pending) == 1
    assert pending[0]["revision_count"] == 2
    assert pending[0]["sent"] == 0
    assert pending[0]["updated_at"]


def test_v1_database_hash_baseline_does_not_requeue(tmp_path, monkeypatch) -> None:
    _use_temp_database(tmp_path, monkeypatch)
    db.save_notice(_notice(), "hash-v1")
    notice_id = db.get_unsent_notices()[0]["id"]
    db.ensure_delivery("key-1", "<message-1@test>", [notice_id])
    db.complete_delivery("key-1", [notice_id])
    with db.connect() as conn:
        conn.execute("UPDATE notices SET content_hash = '' WHERE id = ?", (notice_id,))

    assert db.save_notice(_notice(), "hash-v1") == "baseline"
    assert db.get_unsent_notices() == []


def test_delivery_completion_does_not_acknowledge_restored_content_revision(tmp_path, monkeypatch):
    """正文改回旧内容时哈希相同，但新的修订仍不能由旧投递确认。"""
    _use_temp_database(tmp_path, monkeypatch)
    db.save_notice(_notice(), "hash-v1")
    notice_id = db.get_unsent_notices()[0]["id"]
    db.ensure_delivery("old-delivery", "<old@test>", [notice_id])
    assert db.save_notice(_notice(body="临时修改"), "hash-v2") == "updated"
    assert db.save_notice(_notice(), "hash-v1") == "updated"
    # 模拟恢复任务再次查找同一个旧投递；首次记录的快照不能被覆盖。
    db.ensure_delivery("old-delivery", "<old@test>", [notice_id])
    db.complete_delivery("old-delivery", [notice_id])

    pending = db.get_unsent_notices()
    assert len(pending) == 1, "旧投递不能确认内容相同但修订号已变化的通知"
    assert pending[0]["revision_count"] == 3
    assert pending[0]["sent_at"] is None


def test_legacy_delivery_without_snapshot_cannot_acknowledge_current_revision(
    tmp_path, monkeypatch
):
    """旧表迁移不能将当前版本猜测成历史投递版本。"""
    _use_temp_database(tmp_path, monkeypatch)
    db.save_notice(_notice(), "hash-v1")
    notice_id = db.get_unsent_notices()[0]["id"]
    db.ensure_delivery("legacy", "<legacy@test>", [notice_id])
    with db.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(mail_deliveries)")}
        if "notice_versions_json" in columns:
            conn.execute("ALTER TABLE mail_deliveries DROP COLUMN notice_versions_json")
    db.init_db()
    db.init_db()
    db.complete_delivery("legacy", [notice_id])

    assert [notice["id"] for notice in db.get_unsent_notices()] == [notice_id]
    assert db.get_delivery("legacy")["status"] == "sent"
