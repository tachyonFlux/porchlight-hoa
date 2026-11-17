"""Deployment helpers: database backups, the setup-token banner, host settings."""

import sqlite3
from datetime import datetime

import pytest
from django.core.management import call_command

from apps.core import backup
from apps.core.models import HOASettings


@pytest.fixture
def sqlite_file(tmp_path, settings, monkeypatch):
    db = tmp_path / "db.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("create table t (x int)")
    conn.execute("insert into t values (42)")
    conn.commit()
    conn.close()
    settings.DATA_DIR = tmp_path
    monkeypatch.setattr(backup, "_db_path", lambda: str(db))
    monkeypatch.setattr(backup, "is_sqlite", lambda: True)
    return db


def test_backup_is_consistent_and_pruned(sqlite_file, tmp_path):
    for day in range(1, 5):
        backup.backup_sqlite(keep=2, now=datetime(2026, 1, day))
    files = sorted((tmp_path / "backups").glob("db-*.sqlite3"))
    assert [f.name for f in files] == ["db-20260103-000000.sqlite3", "db-20260104-000000.sqlite3"]
    assert sqlite3.connect(files[-1]).execute("select x from t").fetchone() == (42,)
    assert oct(files[-1].stat().st_mode & 0o777) == "0o600"
    assert not list((tmp_path / "backups").glob("*.partial"))


def test_manual_backup_never_prunes(sqlite_file, tmp_path):
    for day in range(1, 4):
        backup.backup_sqlite(keep=1, now=datetime(2026, 1, day))
    call_command("backup")
    assert len(list((tmp_path / "backups").glob("db-*.sqlite3"))) == 1
    manual = list((tmp_path / "backups").glob("manual-*.sqlite3"))
    assert len(manual) == 1
    # Later daily backups don't delete the manual one.
    backup.backup_sqlite(keep=1, now=datetime(2026, 2, 1))
    assert list((tmp_path / "backups").glob("manual-*.sqlite3")) == manual


def test_stale_partial_files_are_removed(sqlite_file, tmp_path):
    (tmp_path / "backups").mkdir()
    (tmp_path / "backups" / "db-20250101-000000.partial").write_text("half")
    backup.backup_sqlite(keep=5, now=datetime(2026, 1, 1))
    assert not list((tmp_path / "backups").glob("*.partial"))


def test_failed_backup_leaves_no_partial(sqlite_file, tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "_db_path", lambda: str(tmp_path / "missing" / "nope.sqlite3"))
    with pytest.raises(sqlite3.Error):
        backup.backup_sqlite(keep=5, now=datetime(2026, 1, 1))
    assert not list((tmp_path / "backups").glob("*"))


def test_daily_task_backs_up(sqlite_file, tmp_path, settings):
    from apps.core import tasks

    settings.BACKUP_KEEP = 3
    result = tasks.daily(None)
    assert result["backup"].startswith("db-")


def test_daily_task_skips_postgres(settings, monkeypatch):
    from apps.core import tasks

    settings.BACKUP_KEEP = 3
    monkeypatch.setattr(backup, "is_sqlite", lambda: False)
    assert "backup" not in tasks.daily(None)


def test_setup_token_banner(capsys, db, settings, tmp_path):
    settings.DATA_DIR = tmp_path
    settings.SETUP_TOKEN = "s3cret-token"
    hoa = HOASettings.load()
    hoa.setup_complete = False
    hoa.save()
    call_command("show_setup_token")
    assert "s3cret-token" in capsys.readouterr().out
    hoa.setup_complete = True
    hoa.save()
    call_command("show_setup_token")
    assert capsys.readouterr().out == ""


def test_base_url_host_is_allowed(monkeypatch):
    import importlib

    import porchlight.settings as prod

    monkeypatch.setenv("PORCHLIGHT_SECRET_KEY", "x" * 50)
    monkeypatch.setenv("PORCHLIGHT_BASE_URL", "https://hoa.example.org/")
    monkeypatch.setenv("PORCHLIGHT_ALLOWED_HOSTS", "192.168.1.20")
    monkeypatch.delenv("PORCHLIGHT_DEBUG", raising=False)
    try:
        reloaded = importlib.reload(prod)
        assert reloaded.ALLOWED_HOSTS == ["192.168.1.20", "hoa.example.org", "localhost", "127.0.0.1"]
        assert reloaded.CSRF_TRUSTED_ORIGINS == ["https://hoa.example.org"]
    finally:
        monkeypatch.undo()
        importlib.reload(prod)
