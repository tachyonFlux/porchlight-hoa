from . import services


def daily(now):
    return {"share_access_rows_purged": services.purge_access_logs(now)}
