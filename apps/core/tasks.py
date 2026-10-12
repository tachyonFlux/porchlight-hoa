from apps.core import ratelimit


def daily(now):
    return {"ratelimit_rows_deleted": ratelimit.cleanup()}
