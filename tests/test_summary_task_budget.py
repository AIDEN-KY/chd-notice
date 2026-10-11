"""时间预算与摘要并发契约：假摘要、假 SMTP、临时数据库。"""

import asyncio
from types import SimpleNamespace

import db
import service
from tests.test_delivery_recipients import add_notice, register_pair
from tests.test_delivery_recipients import delivery_setup as _delivery_setup
from tests.test_service import _run_immediate

delivery_setup = _delivery_setup


def test_expired_task_budget_stops_before_smtp_and_preserves_pending(delivery_setup):
    register_pair()
    add_notice()
    clock = SimpleNamespace(now=100.0)
    delivery_setup.setattr(
        service, "time", SimpleNamespace(monotonic=lambda: clock.now), raising=False
    )
    service.settings.task_time_budget_seconds = 1
    service.settings.summary_concurrency = 2
    envelopes = []

    async def summarize(*_args):
        clock.now += 2
        return "Fixed summary"

    def send_digest(notices, *_args, recipients, **_kwargs):
        envelopes.append(notices)
        return {"accepted": list(recipients), "refused": {}}

    delivery_setup.setattr(service, "summarize", summarize)
    delivery_setup.setattr(service, "send_digest", send_digest)
    _run_immediate(service.send_pending_today())
    assert envelopes == [], "摘要结束时预算已耗尽，不得领取收件人或启动 SMTP"
    with db.connect() as connection:
        assert connection.execute("SELECT sent FROM notices").fetchone()[0] == 0
        assert connection.execute("SELECT SUM(attempts) FROM batch_recipients").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM daily_digest_reservations").fetchone()[0] == 0
        )


def test_summary_requests_run_concurrently_but_never_exceed_limit(delivery_setup):
    register_pair()
    for index in range(7):
        add_notice(f"https://example.test/concurrency/{index}")
    service.settings.summary_concurrency = 2
    service.settings.task_time_budget_seconds = 60
    active = 0
    peak = 0
    completed = 0
    envelopes = []

    async def summarize(*_args):
        nonlocal active, peak, completed
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0)
            completed += 1
            return "Fixed summary"
        finally:
            active -= 1

    def send_digest(notices, *_args, recipients, **_kwargs):
        assert completed == 7, "所有摘要完成前不得发送不完整日报"
        envelopes.append([notice["id"] for notice in notices])
        return {"accepted": list(recipients), "refused": {}}

    delivery_setup.setattr(service, "summarize", summarize)
    delivery_setup.setattr(service, "send_digest", send_digest)
    asyncio.run(service.send_pending_today())
    assert peak == 2, "配置 2 路并发时不能退化为串行，也不能无界启动请求"
    assert active == 0
    assert envelopes == [list(range(1, 8))]


def test_stalled_summary_is_cancelled_without_consuming_delivery_budget(delivery_setup):
    register_pair()
    add_notice()
    service.settings.task_time_budget_seconds = 0.2
    service.settings.summary_concurrency = 2
    stopped = []

    async def summarize(*_args):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(True)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("超时的摘要不得启动 SMTP")

    delivery_setup.setattr(service, "summarize", summarize)
    delivery_setup.setattr(service, "send_digest", forbidden)
    asyncio.run(service.send_pending_today())
    assert stopped == [True]
    with db.connect() as connection:
        assert connection.execute("SELECT sent FROM notices").fetchone()[0] == 0
        assert connection.execute("SELECT SUM(attempts) FROM batch_recipients").fetchone()[0] == 0


def test_daily_budget_cancels_stalled_scrape_and_records_degraded(delivery_setup):
    register_pair()
    add_notice()
    service.settings.task_time_budget_seconds = 0.2
    stopped = []

    async def scrape(**_kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(True)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("抓取耗尽预算后不得继续发送")

    delivery_setup.setattr(service, "scrape_notices", scrape)
    delivery_setup.setattr(service, "send_digest", forbidden)
    assert asyncio.run(service.run_daily()) == 3
    assert stopped == [True]
    health = db.get_task_health()["latest"]
    assert health["status"] == "degraded"
    assert health["error_type"] == "TaskBudgetExceeded"
    with db.connect() as connection:
        assert connection.execute("SELECT sent FROM notices").fetchone()[0] == 0


def test_budget_after_completed_smtp_preserves_success_and_defers_next_batch(delivery_setup):
    register_pair()
    service.settings.max_notices_per_digest = 1
    service.settings.max_digest_messages_per_day = 2
    service.settings.task_time_budget_seconds = 1
    add_notice("https://example.test/first")
    db.prepare_recipient_batches(db.get_active_users())
    add_notice("https://example.test/second")
    db.prepare_recipient_batches(db.get_active_users())
    clock = SimpleNamespace(now=100.0)
    delivery_setup.setattr(service, "time", SimpleNamespace(monotonic=lambda: clock.now))
    envelopes = []

    def send_digest(notices, *_args, recipients, **_kwargs):
        clock.now += 2
        envelopes.append(notices[0]["id"])
        return {"accepted": list(recipients), "refused": {}}

    delivery_setup.setattr(service, "send_digest", send_digest)
    assert _run_immediate(service.send_pending_today()) == 1
    assert envelopes == [1]
    with db.connect() as connection:
        assert [row[0] for row in connection.execute("SELECT sent FROM notices ORDER BY id")] == [
            1,
            0,
        ]
