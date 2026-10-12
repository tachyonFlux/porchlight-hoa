"""Regression tests for issues found in the slice-2 cross review."""

from datetime import date, timedelta

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import BoardPosition
from apps.community.models import Home, Residency
from apps.dues import services
from apps.dues.models import BillingPeriod, Invoice, LedgerEntry, ReminderLog

from .conftest import DUE, ISSUE_DAY, LATE_AFTER

pytestmark = pytest.mark.django_db


def pay(home, cents, on):
    return services.record_payment(home, cents, "check", on, check_number="1")


@pytest.fixture
def billed(dues, home, resident):
    services.generate_invoices(ISSUE_DAY)
    return Invoice.objects.get(home=home)


class TestNoDoubleBilling:
    def test_switching_to_quarterly_mid_year_does_not_rebill(self, billed, dues, home):
        dues.frequency = "quarterly"
        dues.save()
        for day in (date(2027, 3, 2), date(2027, 6, 1), date(2027, 9, 1)):
            services.generate_invoices(day)
        assert Invoice.objects.filter(home=home).count() == 1  # 2027 annual still covers these
        services.generate_invoices(date(2027, 12, 2))
        new = BillingPeriod.objects.get(start=date(2028, 1, 1))
        assert new.end == date(2028, 3, 31)
        assert services.balance(home) == 60000

    def test_backdated_billing_start_bills_only_recent_period(self, dues, home, resident):
        dues.billing_start_date = date(2024, 1, 1)
        dues.save()
        services.generate_invoices(ISSUE_DAY)
        assert list(BillingPeriod.objects.values_list("start", flat=True)) == [date(2027, 1, 1)]

    def test_reactivated_home_not_billed_for_old_periods(self, billed, other_home):
        Home.objects.filter(pk=other_home.pk).update(is_active=False)
        Invoice.objects.filter(home=other_home).delete()  # never billed while inactive
        LedgerEntry.objects.filter(home=other_home).delete()
        Home.objects.filter(pk=other_home.pk).update(is_active=True)
        services.generate_invoices(date(2027, 6, 1))
        assert not Invoice.objects.filter(home=other_home).exists()

    def test_manual_overlapping_period_rejected(self, login, board_user, billed, dues):
        dues.frequency = "quarterly"
        dues.save()
        response = login(board_user).post(reverse("dues:period_list"), {"date": "2027-05-01"})
        assert response.status_code == 302
        assert BillingPeriod.objects.count() == 1


class TestLateFeeFixes:
    def test_no_fee_for_inactive_home(self, billed, home):
        Home.objects.filter(pk=home.pk).update(is_active=False)
        assert services.apply_late_fees(LATE_AFTER + timedelta(days=1))["late_fees_applied"] == 0

    def test_bounced_check_voided_after_deadline_still_late(self, billed, home):
        payment = pay(home, 30000, date(2027, 1, 15))
        services.void_payment(payment, "Bounced")
        LedgerEntry.objects.filter(kind="payment_void").update(date=LATE_AFTER + timedelta(days=5))
        assert services.apply_late_fees(LATE_AFTER + timedelta(days=6))["late_fees_applied"] == 1

    def test_old_charge_does_not_make_on_time_dues_late(self, billed, home):
        services.add_adjustment(home, 10000, "Opening balance", date(2026, 6, 1))
        pay(home, 30000, DUE)
        assert services.apply_late_fees(LATE_AFTER + timedelta(days=1))["late_fees_applied"] == 0
        assert services.balance(home) == 10000

    def test_fee_terms_snapshotted(self, billed, dues):
        dues.late_fee_flat_cents = 25000
        dues.save()
        services.apply_late_fees(LATE_AFTER + timedelta(days=1))
        assert LedgerEntry.objects.get(kind="late_fee").amount_cents == 2500

    def test_no_fee_long_after_deadline(self, billed):
        assert services.apply_late_fees(LATE_AFTER + timedelta(days=200))["late_fees_applied"] == 0


class TestIssuedEmailRetry:
    def test_retried_when_first_send_fails(self, dues, home, resident, monkeypatch):
        def boom(*a, **k):
            raise ConnectionError("smtp down")

        monkeypatch.setattr(services, "send_templated", boom)
        services.generate_invoices(ISSUE_DAY)
        monkeypatch.undo()
        assert not ReminderLog.objects.exists()
        result = services.send_reminders(ISSUE_DAY + timedelta(days=1))
        assert result["issued"] == 1 and len(mail.outbox) == 1

    def test_household_that_joins_later_gets_invoice(self, dues, home, make_user):
        services.generate_invoices(ISSUE_DAY)  # nobody to email yet
        Residency.objects.create(home=home, user=make_user("late@example.com"))
        services.send_reminders(ISSUE_DAY + timedelta(days=3))
        assert [m.to for m in mail.outbox] == [["late@example.com"]]


class TestPublicInvoicePage:
    def test_no_account_balance_and_private_headers(self, client, billed, home):
        services.add_adjustment(home, 123456, "Fine", date(2026, 12, 3))
        response = client.get(billed.get_public_url())
        assert b"1,234.56" not in response.content and b"1,534.56" not in response.content
        assert response["Cache-Control"] == "private, no-store"
        assert "noindex" in response["X-Robots-Tag"]

    def test_link_expires(self, client, billed, monkeypatch):
        later = billed.period.end + timedelta(days=91)
        monkeypatch.setattr(timezone, "localdate", lambda *a: later)
        assert client.get(billed.get_public_url()).status_code == 410


class TestBoardPowers:
    def test_only_treasurer_or_admin_edits_dues_settings(self, login, board_user, make_user, dues):
        secretary = make_user("sec@example.com")
        BoardPosition.objects.create(user=secretary, title="secretary")
        assert login(secretary).get(reverse("dues:settings")).status_code == 403
        assert login(board_user).get(reverse("dues:settings")).status_code == 200  # treasurer

    def test_payment_instruction_change_alerts_board(
        self, login, board_user, admin_user, dues, django_capture_on_commit_callbacks
    ):
        data = {
            "amount_cents": "300",
            "frequency": "annual",
            "anchor_month": 1,
            "due_day": 1,
            "grace_days": 30,
            "late_fee_type": "flat",
            "late_fee_flat_cents": "25",
            "late_fee_percent": "0",
            "payable_to": "Somebody Else",
            "payment_instructions": "",
            "billing_start_date": "2027-01-01",
            "invoice_lead_days": 30,
            "reminder_days_before": 14,
            "overdue_reminder_every_days": 30,
            "overdue_reminder_max": 3,
        }
        with django_capture_on_commit_callbacks(execute=True):
            login(board_user).post(reverse("dues:settings"), data)
        alert = [m for m in mail.outbox if "payment instructions" in m.subject.lower()]
        assert alert and alert[0].to == [admin_user.email]

    def test_ledger_change_on_own_home_alerts_board(
        self, board_user, admin_user, home, django_capture_on_commit_callbacks
    ):
        Residency.objects.create(home=home, user=board_user)
        with django_capture_on_commit_callbacks(execute=True):
            services.add_adjustment(home, -5000, "credit", date(2026, 10, 1), actor=board_user)
        assert any(admin_user.email in m.to for m in mail.outbox)

    def test_board_password_only_blocked_everywhere_when_two_step_required(
        self, login, board_user, billed, hoa
    ):
        hoa.require_board_two_step = True
        hoa.save()
        client = login(board_user)
        response = client.get(reverse("dues:invoice_detail", args=[billed.pk]))
        assert response.status_code == 302 and response.url == reverse("accounts:totp_setup")
        assert client.get(billed.home.get_absolute_url()).status_code == 302


def test_pdf_never_fetches_external_resources(login, board_user, billed, home, monkeypatch):
    from weasyprint.urls import URLFetcher

    calls = []
    monkeypatch.setattr(URLFetcher, "open", lambda self, *a, **k: calls.append(a))
    Home.objects.filter(pk=home.pk).update(address='<img src="http://127.0.0.1:9/x">')
    response = login(board_user).get(reverse("dues:invoice_pdf", args=[billed.pk]))
    assert response.content.startswith(b"%PDF") and calls == []


def test_my_account_lists_all_open_invoices(login, dues, home, resident):
    dues.frequency = "monthly"
    dues.billing_start_date = date(2027, 1, 1)
    dues.invoice_lead_days = 0
    dues.save()
    for month in range(1, 15):
        day = date(2027 + (month - 1) // 12, (month - 1) % 12 + 1, 1)
        services.generate_invoices(day)
    response = login(resident).get(reverse("dues:my_account"))
    assert len(response.context["accounts"][0]["open"]) == 14
