"""Long-running loop for the scheduler container: runs daily_tasks once a day."""

import logging
import os
import signal
import threading
import time
from datetime import timedelta
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from apps.core.models import TaskRun

from .daily_tasks import run_daily_tasks

log = logging.getLogger("porchlight.scheduler")

MAX_ATTEMPTS_PER_DAY = 3
RETRY_AFTER = timedelta(hours=1)


def should_run(now, hour):
    """Run once per day after `hour`; if that run failed, retry up to 3 times an hour apart.

    All tasks are idempotent, so a retry only redoes work that didn't finish.
    """
    window_start = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if now < window_start:
        window_start -= timedelta(days=1)
    runs = list(TaskRun.objects.filter(started_at__gte=window_start).order_by("-started_at"))
    if not runs:
        return True
    if any(r.ok for r in runs) or len(runs) >= MAX_ATTEMPTS_PER_DAY:
        return False
    return now - runs[0].started_at >= RETRY_AFTER


HEARTBEAT = Path(os.environ.get("PORCHLIGHT_HEARTBEAT_FILE", "/tmp/porchlight-scheduler-heartbeat"))  # noqa: S108


def _heartbeat():
    """The container health check looks at this file's age."""
    try:
        HEARTBEAT.touch()
    except OSError:
        pass


class _KeepAlive:
    """Keep the heartbeat fresh while daily tasks run (PDFs and email for every home can take
    a while on a small NAS), up to a limit so a truly stuck run still shows as unhealthy."""

    def __init__(self, limit_seconds=3 * 3600):
        self.limit = limit_seconds
        self.done = threading.Event()

    def __enter__(self):
        def beat():
            deadline = time.monotonic() + self.limit
            while not self.done.wait(60) and time.monotonic() < deadline:
                _heartbeat()

        threading.Thread(target=beat, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.done.set()


class Command(BaseCommand):
    help = "Run daily_tasks every day at PORCHLIGHT_DAILY_TASKS_HOUR (local time, default 6)."

    def handle(self, *args, **options):
        hour = int(os.environ.get("PORCHLIGHT_DAILY_TASKS_HOUR", "6"))
        stop = {"flag": False}

        def _stop(*_):
            stop["flag"] = True

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        self.stdout.write(f"Scheduler started; daily tasks run at {hour:02d}:00 local time.")
        _heartbeat()

        while not stop["flag"]:
            # Drop stale/broken connections (e.g. Postgres restarted) before each check.
            close_old_connections()
            try:
                if should_run(timezone.localtime(), hour):
                    with _KeepAlive():
                        run = run_daily_tasks()
                    if run is not None:
                        status = "ok" if run.ok else "with errors"
                        self.stdout.write(f"{timezone.localtime():%Y-%m-%d %H:%M} daily tasks {status}")
            except Exception:
                # e.g. "database is locked" during a long import; try again next minute.
                log.exception("Scheduler check failed; will retry")
            _heartbeat()
            for _ in range(60):  # check once a minute, but stop promptly on SIGTERM
                if stop["flag"]:
                    break
                time.sleep(1)
        self.stdout.write("Scheduler stopped.")
