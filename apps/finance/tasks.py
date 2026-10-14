import time
from pathlib import Path

from django.conf import settings

from . import services, simplefin


def _clean_pending_uploads(max_age_hours=24):
    """Uploaded CSVs waiting for column mapping are deleted after a day."""
    folder = Path(settings.DATA_DIR) / "imports" / "pending"
    removed = 0
    if folder.exists():
        cutoff = time.time() - max_age_hours * 3600
        for path in folder.glob("*.csv"):
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
    return removed


def daily(now):
    result = {"pending_uploads_removed": _clean_pending_uploads()}
    if simplefin.is_enabled():
        result.update(services.simplefin_sync())
    return result
