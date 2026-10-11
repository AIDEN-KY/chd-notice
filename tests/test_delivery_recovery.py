"""投递领取与中断恢复的回归契约；仅使用临时 SQLite。"""

import subprocess
import sys

import pytest

import db
from tests.test_delivery_recipients import add_notice, register_pair
from tests.test_delivery_recipients import delivery_setup as _delivery_setup

delivery_setup = _delivery_setup


def test_recovery_cli_initializes_and_lists_without_sending(delivery_setup, capsys):
    import main

    delivery_setup.setattr(main, "settings", db.settings)
    delivery_setup.setattr(main, "configure_logging", lambda: None)
    delivery_setup.setattr(sys, "argv", ["main.py", "deliveries", "--action", "list"])
    main.main()
    assert capsys.readouterr().out == ""
    batch_id, _ = _claimed_batch()
    main.main()
    output = capsys.readouterr().out
    assert f"批次={batch_id}" in output
    assert "InterruptedTask" in output
    assert "@" not in output
    assert db.get_retryable_batches() == []


def test_independent_processes_can_only_claim_once(delivery_setup):
    register_pair()
    add_notice()
    db.prepare_recipient_batches(db.get_active_users())
    batch_id = db.get_retryable_batches()[0]["id"]
    script = """
import sys
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import db
db.settings = SimpleNamespace(database_file=Path(sys.argv[1]), timezone=ZoneInfo('Asia/Shanghai'))
print(len(db.claim_batch_recipients(int(sys.argv[2]))))
"""
    children = []
    try:
        for _ in range(2):
            children.append(
                subprocess.Popen(
                    [sys.executable, "-c", script, str(db.settings.database_file), str(batch_id)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        counts = []
        for child in children:
            stdout, stderr = child.communicate(timeout=15)
            assert child.returncode == 0, stderr
            counts.append(int(stdout.strip()))
        assert sorted(counts) == [0, 2]
        with db.connect() as conn:
            assert (
                conn.execute(
                    "SELECT status FROM digest_batches WHERE id=?", (batch_id,)
                ).fetchone()[0]
                == "sending"
            )
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)


def test_manual_and_daily_tasks_reject_shared_lock_without_smtp(delivery_setup):
    import service
    from task_lock import TaskAlreadyRunningError, task_lock
    from tests.test_service import _run_immediate

    with task_lock(db.settings.database_file.with_suffix(".task.lock")):
        with pytest.raises(TaskAlreadyRunningError):
            _run_immediate(service.send_pending_today())
        with pytest.raises(TaskAlreadyRunningError):
            _run_immediate(service.run_daily())


def _claimed_batch():
    register_pair()
    add_notice()
    db.prepare_recipient_batches(db.get_active_users())
    batch_id = db.get_retryable_batches()[0]["id"]
    return batch_id, db.claim_batch_recipients(batch_id)


def test_duplicate_claim_preserves_in_flight_batch_state(delivery_setup):
    batch_id, recipients = _claimed_batch()
    assert len(recipients) == 2
    assert db.claim_batch_recipients(batch_id) == []
    with db.connect() as conn:
        batch = conn.execute("SELECT status FROM digest_batches WHERE id=?", (batch_id,)).fetchone()
        rows = conn.execute(
            "SELECT status,attempts FROM batch_recipients WHERE batch_id=?", (batch_id,)
        ).fetchall()
    assert batch["status"] == "sending"
    assert [(row["status"], row["attempts"]) for row in rows] == [("sending", 1)] * 2


def test_interrupted_delivery_becomes_unknown_without_automatic_retry(delivery_setup):
    batch_id, recipients = _claimed_batch()
    accepted, interrupted = recipients
    db.record_recipient_result(
        batch_id, [accepted], {"accepted": [accepted["email"]], "refused": {}}
    )
    snapshot = db.get_batch_notices(batch_id)
    # 契约：只能在取得任务锁后调用恢复；没有 SMTP 结果不能推断失败或成功。
    db.recover_interrupted_batches()
    with db.connect() as conn:
        states = {
            row["user_id"]: (row["status"], row["attempts"])
            for row in conn.execute("SELECT * FROM batch_recipients WHERE batch_id=?", (batch_id,))
        }
        ledger = {
            row["user_id"]: row["status"]
            for row in conn.execute(
                "SELECT * FROM user_notice_deliveries WHERE batch_id=?", (batch_id,)
            )
        }
    assert states[accepted["user_id"]] == ("accepted", 1)
    assert states[interrupted["user_id"]] == ("unknown", 1)
    assert ledger == {accepted["user_id"]: "accepted", interrupted["user_id"]: "unknown"}
    assert db.get_retryable_batches() == []
    assert db.get_batch_notices(batch_id) == snapshot
    db.recover_interrupted_batches()
    assert db.get_retryable_batches() == []


@pytest.mark.parametrize("decision", ["confirm", "retry"])
def test_explicit_unknown_resolution_preserves_accepted_and_snapshot(delivery_setup, decision):
    batch_id, recipients = _claimed_batch()
    accepted, unknown = recipients
    db.record_recipient_result(
        batch_id, [accepted], {"accepted": [accepted["email"]], "refused": {}}
    )
    db.record_recipient_result(batch_id, [unknown], None, error="TimeoutError")
    snapshot = db.get_batch_notices(batch_id)
    db.save_notice({**snapshot[0], "content_html": "New revision"}, "hash-v2")
    db.resolve_unknown_delivery(
        batch_id, unknown["user_id"], decision=decision, reason="offline verification"
    )
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT user_id,status,attempts FROM batch_recipients WHERE batch_id=? ORDER BY user_id",
            (batch_id,),
        ).fetchall()
        audit = conn.execute("SELECT * FROM delivery_resolutions").fetchone()
        notice = conn.execute("SELECT sent,revision_count FROM notices").fetchone()
    assert rows[0]["status"] == "accepted"
    assert rows[1]["status"] == ("accepted" if decision == "confirm" else "pending")
    assert [row["attempts"] for row in rows] == [1, 1]
    assert audit["decision"] == decision
    assert audit["reason"] == "offline verification"
    assert notice["sent"] == 0
    assert notice["revision_count"] == 2
    assert db.get_batch_notices(batch_id) == snapshot
    if decision == "retry":
        assert [row["user_id"] for row in db.claim_batch_recipients(batch_id)] == [
            unknown["user_id"]
        ]


def test_resolution_requires_reason_and_unknown_state(delivery_setup):
    batch_id, recipients = _claimed_batch()
    with pytest.raises(ValueError):
        db.resolve_unknown_delivery(
            batch_id, recipients[0]["user_id"], decision="confirm", reason="verified"
        )
    db.record_recipient_result(batch_id, recipients, None)
    with pytest.raises(ValueError):
        db.resolve_unknown_delivery(
            batch_id, recipients[0]["user_id"], decision="retry", reason=" "
        )
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM delivery_resolutions").fetchone()[0] == 0
