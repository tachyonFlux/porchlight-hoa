"""Long-running loop for the scheduler container: runs daily_tasks once a day."""

import os
import signal
import time
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.core.models import TaskRun

from .daily_tasks import run_daily_tasks


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

        while not stop["flag"]:
            now = timezone.localtime()
            last = TaskRun.objects.filter(ok=True).first()
            due_today = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            if now < due_today:
                due_today -= timedelta(days=1)
            latest = TaskRun.objects.first()
            retry_wait = (
                latest is not None
                and not latest.ok
                and (timezone.now() - latest.started_at < timedelta(hours=1))
            )
            needs_run = last is None or timezone.localtime(last.started_at) < due_today
            if needs_run and not retry_wait:
                run = run_daily_tasks()
                if run is not None:
                    status = "ok" if run.ok else "with errors"
                    self.stdout.write(f"{timezone.localtime():%Y-%m-%d %H:%M} daily tasks {status}")
            for _ in range(60):  # check once a minute, but stop promptly on SIGTERM
                if stop["flag"]:
                    break
                time.sleep(1)
        self.stdout.write("Scheduler stopped.")
