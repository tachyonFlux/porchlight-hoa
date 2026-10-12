"""Run every app's daily job (invoices, reminders, late fees, alerts, cleanup).

Safe to run more than once a day: each task is idempotent.
"""

import importlib
import logging
import traceback
from datetime import timedelta

from django.apps import apps
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.core.models import TaskRun

log = logging.getLogger("porchlight.tasks")


def task_modules():
    for config in apps.get_app_configs():
        if not config.name.startswith("apps."):
            continue
        try:
            module = importlib.import_module(f"{config.name}.tasks")
        except ModuleNotFoundError as exc:
            if exc.name == f"{config.name}.tasks":
                continue
            raise
        if hasattr(module, "daily"):
            yield config.label, module.daily


def run_daily_tasks(now=None, force=False):
    now = now or timezone.now()
    running = TaskRun.objects.filter(finished_at__isnull=True, started_at__gt=now - timedelta(hours=1))
    if running.exists() and not force:
        log.warning("daily_tasks already running; skipping")
        return None
    run = TaskRun.objects.create(started_at=now)
    summary, ok = {}, True
    for label, task in task_modules():
        try:
            summary[label] = task(now)
        except Exception:
            ok = False
            summary[label] = {"error": traceback.format_exc(limit=5)}
            log.exception("daily task %s failed", label)
    run.summary, run.ok, run.finished_at = summary, ok, timezone.now()
    run.save()
    return run


class Command(BaseCommand):
    help = "Run Porchlight's scheduled jobs. Idempotent; schedule once a day."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Run even if a run is in progress.")

    def handle(self, *args, force=False, **options):
        run = run_daily_tasks(force=force)
        if run is None:
            self.stdout.write("Another run is in progress; nothing to do.")
            return
        for label, result in run.summary.items():
            self.stdout.write(f"{label}: {result}")
        self.stdout.write(self.style.SUCCESS("done") if run.ok else self.style.ERROR("errors"))
