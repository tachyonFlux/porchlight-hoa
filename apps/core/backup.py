"""Consistent copies of the SQLite database.

Copying db.sqlite3 while the app is writing can produce a broken copy (and the -wal file
holds recent changes). SQLite's online backup API makes a consistent snapshot instead, so
Hyper Backup (or any file backup) should copy DATA_DIR/backups/ and DATA_DIR/media/.
"""

import sqlite3
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.db import connection


def is_sqlite():
    return connection.vendor == "sqlite"


def _db_path():
    return settings.DATABASES["default"]["NAME"]


def backup_dir():
    path = Path(settings.DATA_DIR) / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def backup_sqlite(keep=None, now=None):
    """Write DATA_DIR/backups/db-YYYYMMDD-HHMMSS.sqlite3 and delete all but the newest `keep`.
    Returns the new file's path (None when not using SQLite)."""
    if not is_sqlite():
        return None
    keep = settings.BACKUP_KEEP if keep is None else keep
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    target = backup_dir() / f"db-{stamp}.sqlite3"
    partial = target.with_suffix(".partial")
    source = sqlite3.connect(_db_path())
    try:
        dest = sqlite3.connect(partial)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    partial.replace(target)  # only complete copies get the .sqlite3 name
    target.chmod(0o600)
    old = sorted(backup_dir().glob("db-*.sqlite3"))[:-keep] if keep > 0 else []
    for path in old:
        path.unlink(missing_ok=True)
    return target
