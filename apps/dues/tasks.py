from django.utils import timezone

from . import services


def daily(now):
    today = timezone.localdate(now)
    result = {}
    result.update(services.generate_invoices(today))
    result.update(services.apply_late_fees(today))
    result.update(services.send_reminders(today))
    return result
