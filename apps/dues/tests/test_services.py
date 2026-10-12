"""Ledger services and scheduled jobs: balances, late fees, reminders, idempotency."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core import mail
from django.utils import timezone

from apps.community.models import Home
from apps.core.management.commands.daily_tasks import run_daily_tasks
from apps.dues import services
from apps.dues.models import BillingPeriod, Invoice, LedgerEntry, ReminderLog

from .conftest import DUE, ISSUE_DAY, LATE_AFTER

pytestmark = pytest.mark.django_db


def pay(home, cents, on, **kw):
    return services.record_payment(home, cents, "check", on, check_number=kw.pop("check", "101"), **kw)


@pytest.fixture
def billed(dues, home, resident):
    services.generate_invoices(ISSUE_DAY)
    return Invoice.objects.get(home=home)


class TestInvoiceGeneration:
    def test_nothing_when_disabled(self, dues, home):
        dues.auto_invoicing = False
        dues.save()
        assert services.generate_invoices(ISSUE_DAY) == {"invoices_created": 0}

    def test_not_before_issue_date(self, dues, home):
        assert services.generate_invoices(ISSUE_DAY - timedelta(days=1))["invoices_created"] == 0

    def test_creates_invoice_and_charge(self, billed, home):
        assert billed.amount_cents == 30000 and billed.issued_on == ISSUE_DAY
        assert services.balance(home) == 30000
        assert billed.period.due_date == DUE and billed.period.late_after == LATE_AFTER

    def test_idempotent(self, billed, home, other_home):
        Invoice.objects.filter(home=other_home).delete()  # pretend nothing else changed
        services.generate_invoices(ISSUE_DAY)
        services.generate_invoices(ISSUE_DAY + timedelta(days=3))
        assert Invoice.objects.filter(home=home).count() == 1
        assert LedgerEntry.objects.filter(home=home, kind="invoice").count() == 1
        assert services.balance(home) == 30000

    def test_skips_inactive_homes(self, dues, home):
        Home.objects.create(address="9 Gone Rd", is_active=False)
        services.generate_invoices(ISSUE_DAY)
        assert Invoice.objects.count() == 1

    def test_home_added_after_billing_not_billed_automatically(self, billed):
        late = Home.objects.create(address="1 Brand New Ct")
        later = timezone.make_aware(timezone.datetime(2026, 12, 7, 12, 0))
        Home.objects.filter(pk=late.pk).update(created_at=later)
        services.generate_invoices(ISSUE_DAY + timedelta(days=10))
        assert not Invoice.objects.filter(home=late).exists()

    def test_amount_change_affects_only_new_periods(self, billed, dues):
        dues.amount_cents = 36000
        dues.save()
        services.generate_invoices(date(2027, 12, 2))
        amounts = sorted(Invoice.objects.values_list("amount_cents", flat=True))
        assert amounts == [30000, 36000]

    def test_new_invoice_emailed_once(self, billed, resident):
        assert len(mail.outbox) == 1 and mail.outbox[0].to == [resident.email]
        services.generate_invoices(ISSUE_DAY)
        assert len(mail.outbox) == 1


class TestPaymentsAndAdjustments:
    def test_payment_reduces_balance(self, billed, home):
        pay(home, 30000, date(2026, 12, 20))
        assert services.balance(home) == 0
        assert services.invoice_status(billed, DUE).state == "paid"

    def test_partial_payment(self, billed, home):
        pay(home, 10000, date(2026, 12, 20))
        status = services.invoice_status(billed, DUE)
        assert (status.state, status.unpaid_cents, status.paid_cents) == ("open", 20000, 10000)

    def test_overpayment_becomes_credit(self, billed, home):
        pay(home, 40000, date(2026, 12, 20))
        assert services.balance(home) == -10000

    def test_void_restores_balance_once(self, billed, home, board_user):
        payment = pay(home, 30000, date(2026, 12, 20))
        services.void_payment(payment, "Bounced", actor=board_user)
        services.void_payment(payment, "Bounced again", actor=board_user)
        assert services.balance(home) == 30000
        assert LedgerEntry.objects.filter(kind="payment_void").count() == 1

    def test_adjustments(self, home, board_user):
        services.add_adjustment(home, 15000, "Opening balance", date(2026, 1, 1), actor=board_user)
        services.add_adjustment(home, -5000, "Goodwill credit", date(2026, 2, 1), actor=board_user)
        assert services.balance(home) == 10000
        with pytest.raises(ValueError):
            services.add_adjustment(home, 0, "nothing", date(2026, 2, 1))

    def test_rejects_non_positive_payment(self, home):
        with pytest.raises(ValueError):
            pay(home, 0, date(2026, 12, 20))

    def test_board_actions_are_audited(self, billed, home, board_user):
        from apps.core.models import AuditLog

        payment = pay(home, 100, date(2026, 12, 20), actor=board_user)
        services.void_payment(payment, "typo", actor=board_user)
        actions = set(AuditLog.objects.values_list("action", flat=True))
        assert {"dues.payment_recorded", "dues.payment_voided"} <= actions


class TestLateFees:
    def test_none_during_grace(self, billed, home):
        assert services.apply_late_fees(LATE_AFTER)["late_fees_applied"] == 0

    def test_applied_once_after_grace(self, billed, home):
        assert services.apply_late_fees(LATE_AFTER + timedelta(days=1))["late_fees_applied"] == 1
        assert services.apply_late_fees(LATE_AFTER + timedelta(days=2))["late_fees_applied"] == 0
        fee = LedgerEntry.objects.get(kind="late_fee")
        assert fee.amount_cents == 2500 and fee.date == LATE_AFTER + timedelta(days=1)
        assert services.balance(home) == 32500

    def test_paid_on_time(self, billed, home):
        pay(home, 30000, LATE_AFTER)  # last day of grace counts
        assert services.apply_late_fees(LATE_AFTER + timedelta(days=5))["late_fees_applied"] == 0

    def test_paid_late_but_before_task_ran(self, billed, home):
        pay(home, 30000, LATE_AFTER + timedelta(days=1))
        assert services.apply_late_fees(LATE_AFTER + timedelta(days=10))["late_fees_applied"] == 1

    def test_percent_of_unpaid_portion(self, billed, home):
        BillingPeriod.objects.update(late_fee_type="percent", late_fee_percent=Decimal("10"))
        pay(home, 20000, date(2027, 1, 10))
        services.apply_late_fees(LATE_AFTER + timedelta(days=1))
        assert LedgerEntry.objects.get(kind="late_fee").amount_cents == 1000

    def test_disabled(self, billed):
        BillingPeriod.objects.update(late_fee_type="none")
        assert services.apply_late_fees(LATE_AFTER + timedelta(days=1))["late_fees_applied"] == 0

    def test_credit_from_earlier_covers_invoice(self, dues, home):
        services.add_adjustment(home, -30000, "Prepaid", date(2026, 11, 1))
        services.generate_invoices(ISSUE_DAY)
        assert services.apply_late_fees(LATE_AFTER + timedelta(days=1))["late_fees_applied"] == 0

    def test_invoice_created_after_due_date_gets_no_auto_fee(self, dues, home):
        services.generate_invoices(ISSUE_DAY)
        Invoice.objects.update(created_at=timezone.make_aware(timezone.datetime(2027, 1, 5)))
        assert services.apply_late_fees(LATE_AFTER + timedelta(days=1))["late_fees_applied"] == 0

    def test_waive(self, billed, home, board_user):
        services.apply_late_fees(LATE_AFTER + timedelta(days=1))
        services.waive_late_fee(billed, actor=board_user, reason="First time")
        assert services.waive_late_fee(billed, actor=board_user) is None  # idempotent
        assert services.balance(home) == 30000
        status = services.invoice_status(billed, LATE_AFTER + timedelta(days=2))
        assert status.unpaid_cents == 30000 and status.late_fee_cents == 0

    def test_waive_without_fee(self, billed):
        with pytest.raises(ValueError):
            services.waive_late_fee(billed)


class TestReminders:
    def kinds(self):
        return sorted(ReminderLog.objects.values_list("kind", "sequence"))

    def test_upcoming_once_in_window(self, billed):
        mail.outbox.clear()
        services.send_reminders(DUE - timedelta(days=15))
        assert mail.outbox == []
        services.send_reminders(DUE - timedelta(days=14))
        services.send_reminders(DUE - timedelta(days=13))
        assert len(mail.outbox) == 1 and "reminder" in mail.outbox[0].subject.lower()

    def test_no_reminders_when_paid(self, billed, home):
        pay(home, 30000, date(2026, 12, 10))
        mail.outbox.clear()
        services.send_reminders(DUE - timedelta(days=10))
        services.send_reminders(LATE_AFTER + timedelta(days=40))
        assert mail.outbox == []

    def test_overdue_sequence_and_cap(self, billed):
        mail.outbox.clear()
        services.send_reminders(LATE_AFTER)  # still in grace
        assert mail.outbox == []
        for offset in (1, 2, 31, 40, 61, 91, 200):
            services.send_reminders(LATE_AFTER + timedelta(days=offset))
        overdue = [k for k in self.kinds() if k[0] == "overdue"]
        assert overdue == [("overdue", 0), ("overdue", 1), ("overdue", 2)]
        assert len(mail.outbox) == 3

    def test_long_outage_sends_one_overdue_not_a_backlog(self, billed):
        mail.outbox.clear()
        services.send_reminders(LATE_AFTER + timedelta(days=100))
        assert len(mail.outbox) == 1

    def test_no_recipients_no_log(self, dues, home):
        services.generate_invoices(ISSUE_DAY)  # home has no residents
        services.send_reminders(DUE - timedelta(days=10))
        assert ReminderLog.objects.count() == 0

    def test_failed_send_is_retried(self, billed, monkeypatch):
        def boom(*a, **k):
            raise ConnectionError("smtp down")

        monkeypatch.setattr(services, "send_templated", boom)
        services.send_reminders(DUE - timedelta(days=10))
        assert not ReminderLog.objects.filter(kind="upcoming").exists()
        monkeypatch.undo()
        services.send_reminders(DUE - timedelta(days=9))
        assert ReminderLog.objects.filter(kind="upcoming").exists()


def test_daily_tasks_twice_is_idempotent(dues, home, resident, other_home):
    def at(d):
        return timezone.make_aware(timezone.datetime(d.year, d.month, d.day, 7, 0))

    first = run_daily_tasks(now=at(ISSUE_DAY))
    second = run_daily_tasks(now=at(ISSUE_DAY) + timedelta(minutes=5))
    assert first.ok and second.ok
    assert first.summary["dues"]["invoices_created"] == 2
    assert second.summary["dues"]["invoices_created"] == 0

    late_day = LATE_AFTER + timedelta(days=1)
    third = run_daily_tasks(now=at(late_day))
    fourth = run_daily_tasks(now=at(late_day) + timedelta(minutes=5))
    assert third.summary["dues"]["late_fees_applied"] == 2
    assert fourth.summary["dues"] == {
        "invoices_created": 0,
        "late_fees_applied": 0,
        "issued": 0,
        "upcoming": 0,
        "overdue": 0,
    }
    assert services.balance(home) == 32500
