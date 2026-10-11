"""诊断 CLI 应只读，且不能暗中初始化数据库或运行任务。"""

import sys

import db
import main
from tests.test_config import local_health_configuration as _local_health_configuration

local_health_configuration = _local_health_configuration


def test_doctor_cli_does_not_create_database_or_runtime_directories(
    local_health_configuration, monkeypatch
):
    configuration = local_health_configuration
    monkeypatch.setattr(main, "settings", configuration)
    monkeypatch.setattr(db, "settings", configuration)
    monkeypatch.setattr(main, "configure_logging", lambda: None)
    monkeypatch.setattr(sys, "argv", ["main.py", "doctor"])
    try:
        main.main()
    except SystemExit as exc:
        assert exc.code == 0
    assert not configuration.database_file.exists(), "doctor 不得调用 init_db 创建数据库"
    assert not configuration.attachment_dir.exists(), "诊断不得创建下载目录"
    assert not configuration.log_dir.exists(), "诊断不得创建日志目录"


def test_doctor_existing_database_is_not_migrated(local_health_configuration, monkeypatch):
    configuration = local_health_configuration
    monkeypatch.setattr(db, "settings", configuration)
    db.init_db()
    with db.connect() as connection:
        connection.execute("PRAGMA user_version=0")
    before = configuration.database_file.read_bytes()
    monkeypatch.setattr(main, "settings", configuration)
    monkeypatch.setattr(sys, "argv", ["main.py", "doctor"])
    try:
        main.main()
    except SystemExit as exc:
        assert exc.code == 0
    assert configuration.database_file.read_bytes() == before
    assert not configuration.log_dir.exists()
