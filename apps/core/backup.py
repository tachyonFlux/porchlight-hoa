"""Consistent copies of the SQLite database.

Copying db.sqlite3 while the app is writing can produce a broken copy (and the -wal file
holds recent changes). SQLite's online backup API makes a consistent snapshot instead, so
Hyper Backup (or any file backup) should copy DATA_DIR/backups/ and DATA_DIR/media/.
"""

import os
import sqlite3
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.utils import timezone


def is_sqlite():
    return connection.vendor == "sqlite"


def _db_path():
    return settings.DATABASES["default"]["NAME"]


def backup_dir():
    path = Path(settings.DATA_DIR) / "backups"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def backup_sqlite(keep=None, now=None, prefix="db"):
    """Write DATA_DIR/backups/<prefix>-YYYYMMDD-HHMMSS.sqlite3 (local time). Daily backups
    ("db-") beyond the newest `keep` are deleted; manual ones ("manual-") are never pruned.
    Returns the new file's path (None when not using SQLite)."""
    if not is_sqlite():
        return None
    keep = settings.BACKUP_KEEP if keep is None else keep
    folder = backup_dir()
    for stale in folder.glob("*.partial"):  # left behind by an interrupted backup
        stale.unlink(missing_ok=True)
    stamp = (now or timezone.localtime()).strftime("%Y%m%d-%H%M%S")
    target = folder / f"{prefix}-{stamp}.sqlite3"
    partial = target.with_suffix(".partial")
    # Create it private before any data goes in.
    os.close(os.open(partial, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600))
    try:
        source = sqlite3.connect(_db_path())
        try:
            dest = sqlite3.connect(partial)
            try:
                source.backup(dest)
            finally:
                dest.close()
        finally:
            source.close()
        partial.chmod(0o600)
        partial.replace(target)  # only complete copies get the .sqlite3 name
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    old = sorted(folder.glob("db-*.sqlite3"))[:-keep] if keep > 0 and prefix == "db" else []
    for path in old:
        path.unlink(missing_ok=True)
    return target
