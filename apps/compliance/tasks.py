from django.utils import timezone

from . import services


def daily(now):
    return services.send_reminders(timezone.localdate(now))
