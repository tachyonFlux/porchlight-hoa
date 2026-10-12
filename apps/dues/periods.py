"""Pure date math for billing periods. No database access."""

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

FREQUENCY_MONTHS = {"annual": 12, "semiannual": 6, "quarterly": 3, "monthly": 1}


@dataclass(frozen=True)
class PeriodSpec:
    start: date
    end: date
    due_date: date

    def issue_date(self, lead_days):
        return self.due_date - timedelta(days=lead_days)


def _month_index(d):
    return d.year * 12 + (d.month - 1)


def _from_index(index):
    return date(index // 12, index % 12 + 1, 1)


def add_months(d, months):
    index = _month_index(d) + months
    first = _from_index(index)
    last_day = calendar.monthrange(first.year, first.month)[1]
    return first.replace(day=min(d.day, last_day))


def clamp_day(year, month, day):
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def period_containing(d, frequency, anchor_month, due_day):
    """The billing period that contains date `d`.

    Periods start on the 1st of `anchor_month` and repeat every N months (N from `frequency`).
    The due date is `due_day` of the period's first month (clamped to the month's length).
    """
    months = FREQUENCY_MONTHS[frequency]
    offset = anchor_month - 1
    k = (_month_index(d) - offset) // months
    start = _from_index(offset + k * months)
    end = add_months(start, months) - timedelta(days=1)
    return PeriodSpec(start, end, clamp_day(start.year, start.month, due_day))


def next_period(spec, frequency, anchor_month, due_day):
    return period_containing(spec.end + timedelta(days=1), frequency, anchor_month, due_day)


def periods_to_issue(today, *, frequency, anchor_month, due_day, lead_days, billing_start):
    """All periods that start on/after `billing_start` and whose issue date has arrived.

    Includes missed periods (e.g. if the scheduler was down), oldest first.
    """
    if billing_start is None:
        return []
    spec = period_containing(billing_start, frequency, anchor_month, due_day)
    if spec.start < billing_start:
        spec = next_period(spec, frequency, anchor_month, due_day)
    result = []
    while spec.issue_date(lead_days) <= today:
        result.append(spec)
        spec = next_period(spec, frequency, anchor_month, due_day)
    return result


def period_label(spec, frequency):
    if frequency == "annual":
        if spec.start.month == 1:
            return f"{spec.start.year} dues"
        return f"{spec.start:%b %Y} – {spec.end:%b %Y} dues"
    if frequency == "monthly":
        return f"{spec.start:%B %Y} dues"
    return f"{spec.start:%b} – {spec.end:%b %Y} dues"
