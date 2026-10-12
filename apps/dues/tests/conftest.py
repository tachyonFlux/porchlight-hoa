from datetime import date
from decimal import Decimal

import pytest

from apps.dues.models import DuesSettings


@pytest.fixture
def dues(db):
    s = DuesSettings.load()
    s.auto_invoicing = True
    s.amount_cents = 30000
    s.frequency = "annual"
    s.anchor_month = 1
    s.due_day = 1
    s.invoice_lead_days = 30
    s.billing_start_date = date(2027, 1, 1)
    s.grace_days = 30
    s.late_fee_type = DuesSettings.LateFeeType.FLAT
    s.late_fee_flat_cents = 2500
    s.late_fee_percent = Decimal("0")
    s.payable_to = "Test Hollow HOA"
    s.reminder_days_before = 14
    s.overdue_reminder_every_days = 30
    s.overdue_reminder_max = 3
    s.save()
    return s


ISSUE_DAY = date(2026, 12, 2)  # 30 days before Jan 1 2027
DUE = date(2027, 1, 1)
LATE_AFTER = date(2027, 1, 31)
