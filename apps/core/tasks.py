from django.conf import settings

from apps.core import backup, ratelimit


def daily(now):
    result = {"ratelimit_rows_deleted": ratelimit.cleanup()}
    if settings.BACKUP_KEEP > 0 and backup.is_sqlite():
        result["backup"] = backup.backup_sqlite().name
    return result
