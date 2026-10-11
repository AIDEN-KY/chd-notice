"""积压发送预算契约：仅使用临时数据库及假 SMTP。"""

import db
import service
from tests.test_delivery_recipients import (
    add_notice,
    register_pair,
)
from tests.test_delivery_recipients import (
    delivery_setup as _delivery_setup,
)
from tests.test_service import _run_immediate

delivery_setup = _delivery_setup


def _capture_delivery(monkeypatch):
    envelopes = []

    def send_digest(notices, *_args, recipients, **_kwargs):
        envelopes.append([notice["id"] for notice in notices])
        return {"accepted": list(recipients), "refused": {}}

    monkeypatch.setattr(service, "send_digest", send_digest)
    service.settings.max_notices_per_digest = 2
    service.settings.max_digest_messages_per_day = 1
    return envelopes


def test_backlog_is_bounded_and_remaining_notices_stay_pending(delivery_setup):
    envelopes = _capture_delivery(delivery_setup)
    register_pair()
    for index in range(5):
        add_notice(f"https://example.test/backlog/{index}")

    _run_immediate(service.send_pending_today())

    assert len(envelopes) == 1
    assert len(envelopes[0]) == 2, "每封数量预算应阻止积压全量发送"
    with db.connect() as connection:
        pending = connection.execute("SELECT COUNT(*) FROM notices WHERE sent=0").fetchone()[0]
    assert pending == 3, "超预算通知必须保留待发，不得标记成功"


def test_same_day_second_invocation_does_not_send_another_digest(delivery_setup):
    envelopes = _capture_delivery(delivery_setup)
    register_pair()
    add_notice("https://example.test/first")
    _run_immediate(service.send_pending_today())
    assert len(envelopes) == 1

    add_notice("https://example.test/second")
    _run_immediate(service.send_pending_today())

    assert len(envelopes) == 1, "同一收件人每日预算应跨任务调用生效"
    with db.connect() as connection:
        pending = connection.execute("SELECT COUNT(*) FROM notices WHERE sent=0").fetchone()[0]
    assert pending == 1


def test_deferred_digest_can_be_sent_on_next_day(delivery_setup):
    envelopes = _capture_delivery(delivery_setup)
    register_pair()
    add_notice("https://example.test/first")
    _run_immediate(service.send_pending_today())
    add_notice("https://example.test/second")
    _run_immediate(service.send_pending_today())
    assert len(envelopes) == 1
    with db.connect() as connection:
        connection.execute("UPDATE daily_digest_reservations SET delivery_date='2000-01-01'")
        connection.execute(
            "UPDATE batch_recipients SET updated_at='2000-01-01T21:00:00+08:00' WHERE status='accepted'"
        )
    _run_immediate(service.send_pending_today())
    assert len(envelopes) == 2
