import csv
import io
from datetime import date

import pytest
from django.urls import reverse

from apps.community.models import Home
from apps.dues import services
from apps.dues.models import DuesSettings, Invoice, Payment

from .conftest import ISSUE_DAY

pytestmark = pytest.mark.django_db


@pytest.fixture
def billed(dues, home, other_home, resident, neighbor):
    services.generate_invoices(ISSUE_DAY)
    return Invoice.objects.get(home=home)


class TestResidentViews:
    def test_my_account(self, login, billed, resident):
        response = login(resident).get(reverse("dues:my_account"))
        assert response.status_code == 200
        assert response.context["accounts"][0]["balance"] == 30000
        assert b"$300.00" in response.content

    def test_own_invoice_ok_other_forbidden(self, login, billed, resident, other_home):
        client = login(resident)
        assert client.get(billed.get_absolute_url()).status_code == 200
        other = Invoice.objects.get(home=other_home)
        assert client.get(other.get_absolute_url()).status_code == 403
        assert client.get(reverse("dues:invoice_pdf", args=[other.pk])).status_code == 403

    def test_public_qr_page_needs_no_login(self, client, billed):
        response = client.get(billed.get_public_url())
        assert response.status_code == 200
        assert b"12 Elm Street" in response.content
        assert client.get(reverse("dues:invoice_public", args=["not-a-token"])).status_code == 404

    def test_pdf(self, login, billed, resident):
        response = login(resident).get(reverse("dues:invoice_pdf", args=[billed.pk]))
        assert response["Content-Type"] == "application/pdf"
        assert response.content.startswith(b"%PDF")

    def test_resident_blocked_from_board_pages(self, login, billed, resident):
        client = login(resident)
        for name, args in [
            ("dues:overview", []),
            ("dues:payment_new", []),
            ("dues:settings", []),
            ("dues:home_ledger", [billed.home_id]),
            ("dues:report_csv", []),
        ]:
            assert client.get(reverse(name, args=args)).status_code == 403


class TestBoardViews:
    def test_overview(self, login, board_user, billed):
        response = login(board_user).get(reverse("dues:overview"))
        assert response.status_code == 200
        assert response.context["totals"]["billed"] == 60000
        assert response.context["total_owed"] == 60000

    def test_record_payment_form(self, login, board_user, billed, home):
        client = login(board_user)
        data = {
            "home": home.pk,
            "amount_cents": "$300.00",
            "method": "check",
            "check_number": "",
            "received_on": "2026-10-01",
            "note": "",
        }
        response = client.post(reverse("dues:payment_new"), data)
        assert response.status_code == 200  # check number required for checks
        data["check_number"] = "1042"
        response = client.post(reverse("dues:payment_new"), data)
        assert response.status_code == 302
        payment = Payment.objects.get()
        assert payment.amount_cents == 30000 and payment.recorded_by == board_user
        assert services.balance(home) == 0

    def test_future_payment_date_rejected(self, login, board_user, home):
        response = login(board_user).post(
            reverse("dues:payment_new"),
            {"home": home.pk, "amount_cents": "10", "method": "cash", "received_on": "2999-01-01"},
        )
        assert response.status_code == 200 and not Payment.objects.exists()

    def test_void_and_waive_views(self, login, board_user, billed, home):
        client = login(board_user)
        payment = services.record_payment(home, 5000, "cash", date(2026, 12, 1))
        client.post(reverse("dues:payment_void", args=[payment.pk]), {"reason": "Entered twice"})
        payment.refresh_from_db()
        assert payment.voided_at is not None
        response = client.post(reverse("dues:waive_late_fee", args=[billed.pk]), {"reason": ""})
        assert response.status_code == 302  # no fee: friendly error, no crash

    def test_adjustment_view(self, login, board_user, home):
        login(board_user).post(
            reverse("dues:adjustment", args=[home.pk]),
            {
                "kind": "charge",
                "amount_cents": "125.50",
                "memo": "Opening balance",
                "entry_date": "2026-10-01",
            },
        )
        assert services.balance(home) == 12550

    def test_ledger_page(self, login, board_user, billed, home):
        response = login(board_user).get(reverse("dues:home_ledger", args=[home.pk]))
        assert response.status_code == 200
        assert len(response.context["rows"]) == 1

    def test_csv_export(self, login, board_user, billed, home):
        Home.objects.filter(pk=home.pk).update(address="=HYPERLINK(1)")
        response = login(board_user).get(reverse("dues:report_csv"), {"period": billed.period_id})
        rows = list(csv.DictReader(io.StringIO(response.content.decode())))
        assert {r["address"] for r in rows} == {"'=HYPERLINK(1)", "14 Elm Street"}
        first = next(r for r in rows if r["address"].startswith("'"))
        assert (first["invoiced"], first["unpaid"], first["status"], first["current_balance"]) == (
            "300.00",
            "300.00",
            "open",
            "300.00",
        )

    def test_settings_require_start_date_for_auto(self, login, board_user, dues):
        data = {
            "amount_cents": "300",
            "frequency": "annual",
            "anchor_month": 1,
            "due_day": 1,
            "grace_days": 30,
            "late_fee_type": "none",
            "late_fee_flat_cents": "",
            "late_fee_percent": "0",
            "payable_to": "",
            "payment_instructions": "",
            "auto_invoicing": "on",
            "billing_start_date": "",
            "invoice_lead_days": 30,
            "email_new_invoices": "on",
            "reminder_days_before": 14,
            "overdue_reminder_every_days": 30,
            "overdue_reminder_max": 3,
        }
        client = login(board_user)
        assert client.post(reverse("dues:settings"), data).status_code == 200
        data["billing_start_date"] = "2028-01-01"
        data["amount_cents"] = "325.00"
        assert client.post(reverse("dues:settings"), data).status_code == 302
        assert DuesSettings.load().amount_cents == 32500

    def test_manual_period_and_billing(self, login, board_user, dues, home, other_home):
        dues.auto_invoicing = False
        dues.save()
        client = login(board_user)
        response = client.post(reverse("dues:period_list"), {"date": "2026-10-11"})
        period_url = response.url
        response = client.get(period_url)
        homes = list(response.context["form"].fields["homes"].queryset)
        assert set(homes) == {home, other_home}
        client.post(period_url, {"homes": [home.pk]})
        assert Invoice.objects.filter(home=home).count() == 1
        assert not Invoice.objects.filter(home=other_home).exists()

    def test_bulk_print(self, login, board_user, billed):
        client = login(board_user)
        html = client.get(reverse("dues:period_print", args=[billed.period_id]))
        assert html.content.count(b'class="inv-page"') == 2
        pdf = client.get(
            reverse("dues:period_print", args=[billed.period_id]), {"format": "pdf", "unpaid": "1"}
        )
        assert pdf.content.startswith(b"%PDF")

    def test_dashboard_shows_dues(self, login, board_user, resident, billed):
        assert login(board_user).get("/").context["dues_stats"]["total_owed"] == 60000
        assert login(resident).get("/").context["residencies"][0].balance == 30000
