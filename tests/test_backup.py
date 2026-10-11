"""本地备份恢复 CLI 契约，使用临时库和固定投递事实。"""

import sqlite3
import sys
from contextlib import closing

import pytest

import db
import main
import service
from backup import copy_database
from tests.test_delivery_recipients import add_notice, register_pair
from tests.test_delivery_recipients import delivery_setup as _delivery_setup

delivery_setup = _delivery_setup


def test_database_records_explicit_schema_version(delivery_setup):
    with db.connect() as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert version > 0, "恢复前需要可校验的 schema 版本，当前数据库仅使用默认版本 0"


def _dump(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        return list(connection.iterdump())


def _cli(monkeypatch, arguments):
    monkeypatch.setattr(main, "settings", service.settings)
    monkeypatch.setattr(main, "configure_logging", lambda: None)
    monkeypatch.setattr(sys, "argv", ["main.py", *arguments])
    try:
        main.main()
    except SystemExit as exc:
        assert exc.code in (0, None), "缺少本地备份/恢复命令，或该命令未成功完成"


def test_backup_and_offline_restore_preserve_delivery_facts(delivery_setup, tmp_path):
    users = register_pair()
    add_notice()
    db.prepare_recipient_batches(db.get_active_users())
    with db.connect() as connection:
        for user_id, state in zip(users, ("accepted", "unknown"), strict=True):
            connection.execute(
                "UPDATE batch_recipients SET status=?,attempts=1 WHERE user_id=?", (state, user_id)
            )
            connection.execute(
                "UPDATE user_notice_deliveries SET status=? WHERE user_id=?", (state, user_id)
            )
    source = service.settings.database_file
    source_bytes = source.read_bytes()
    original = _dump(source)
    backup = tmp_path / "snapshot.db"
    restored = tmp_path / "restored.db"
    _cli(delivery_setup, ["backup", "--output", str(backup)])
    assert backup.is_file()
    assert _dump(backup) == original
    _cli(delivery_setup, ["restore", "--input", str(backup), "--output", str(restored)])
    assert _dump(restored) == original
    assert source.read_bytes() == source_bytes, "恢复演练不得覆盖正在使用的数据库"
    with closing(sqlite3.connect(restored)) as connection:
        assert connection.execute(
            "SELECT status FROM batch_recipients ORDER BY user_id"
        ).fetchall() == [("accepted",), ("unknown",)]


def test_backup_refuses_to_overwrite_existing_destination(delivery_setup, tmp_path):
    destination = tmp_path / "existing.db"
    destination.write_bytes(b"keep-existing-file")
    with pytest.raises(ValueError):
        copy_database(service.settings.database_file, destination)
    assert destination.read_bytes() == b"keep-existing-file"
    assert list(tmp_path.glob(".chd-backup-*")) == []


@pytest.mark.parametrize("damaged", [True, False])
def test_invalid_or_future_schema_is_rejected_without_target(delivery_setup, tmp_path, damaged):
    source = service.settings.database_file
    if damaged:
        source.write_bytes(b"not-a-database")
    else:
        with db.connect() as connection:
            connection.execute("PRAGMA user_version=999")
    original = source.read_bytes()
    destination = tmp_path / "rejected.db"
    with pytest.raises((ValueError, sqlite3.Error)):
        copy_database(source, destination)
    assert not destination.exists()
    assert source.read_bytes() == original
    assert list(tmp_path.glob(".chd-backup-*")) == []


def test_backup_includes_committed_wal_data(delivery_setup, tmp_path):
    source = service.settings.database_file
    with closing(sqlite3.connect(source)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE offline_audit (message TEXT)")
        writer.execute("INSERT INTO offline_audit VALUES ('committed-in-WAL')")
        writer.commit()
        destination = tmp_path / "wal-backup.db"
        copy_database(source, destination)
        with closing(sqlite3.connect(destination)) as reader:
            assert (
                reader.execute("SELECT message FROM offline_audit").fetchone()[0]
                == "committed-in-WAL"
            )


def test_repeated_migration_preserves_all_records(delivery_setup):
    register_pair()
    add_notice()
    db.prepare_recipient_batches(db.get_active_users())
    with db.connect() as connection:
        connection.execute("PRAGMA user_version=0")
    db.init_db()
    original = _dump(service.settings.database_file)
    db.init_db()
    assert _dump(service.settings.database_file) == original


def test_restore_cli_refuses_active_database(delivery_setup, tmp_path):
    snapshot = tmp_path / "snapshot.db"
    copy_database(service.settings.database_file, snapshot)
    before = service.settings.database_file.read_bytes()
    delivery_setup.setattr(main, "settings", service.settings)
    delivery_setup.setattr(
        sys,
        "argv",
        [
            "main.py",
            "restore",
            "--input",
            str(snapshot),
            "--output",
            str(service.settings.database_file),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 2
    assert service.settings.database_file.read_bytes() == before


def test_initialization_rejects_future_schema_without_mutation(delivery_setup):
    with db.connect() as connection:
        connection.execute("PRAGMA user_version=999")
    before = service.settings.database_file.read_bytes()
    with pytest.raises(ValueError):
        db.init_db()
    assert service.settings.database_file.read_bytes() == before
