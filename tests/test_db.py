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
    assert failed["last_error"] == "SMTP timeout"

    assert db.start_delivery("key-1") == 2
    db.complete_delivery("key-1", [notice_id])
    completed = db.get_delivery("key-1")
    assert completed is not None
    assert completed["status"] == "sent"
    assert completed["attempts"] == 2
    assert db.get_unsent_notices() == []


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
