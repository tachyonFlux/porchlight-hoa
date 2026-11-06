from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.accounts.models import BoardPosition
from apps.community.models import Home
from apps.finance.models import Account, BankAccount, Transaction
from apps.tax import services
from apps.tax.models import TaxYearConfig, Worksheet

pytestmark = pytest.mark.django_db


def acct(code):
    return Account.objects.get(code=code)


@pytest.fixture
def bank(db):
    return BankAccount.objects.create(name="Operating", opening_date=date(2025, 1, 1))


def txn(bank, cents, code, on=date(2025, 6, 1), n=[0]):  # noqa: B006 - unique-key counter
    n[0] += 1
    return Transaction.objects.create(
        bank_account=bank,
        date=on,
        description="X",
        amount_cents=cents,
        source="csv",
        dedupe_key=f"t{n[0]}",
        account=acct(code) if code else None,
    )


@pytest.fixture
def books(bank):
    """A typical small HOA year: dues, a little interest, ordinary spending."""
    for _ in range(30):
        txn(bank, 300_00, "4000")
    txn(bank, 25_00, "4010")  # late fee: exempt function income
    txn(bank, 640_00, "4100")  # interest: taxable
    txn(bank, -6000_00, "5000")
    txn(bank, -1500_00, "5200")
    txn(bank, -300_00, "5800")  # community events
    txn(bank, -5000_00, "9000")  # transfer to reserves: ignored
    return bank


def test_seeded_rules_match_irs_2025_instructions():
    c = TaxYearConfig.objects.get(year=2025)
    assert c.gross_income_test_pct == 60 and c.expenditure_test_pct == 90 and c.residential_test_pct == 85
    assert c.specific_deduction_cents == 100_00
    assert c.tax_rate_pct == 30 and c.timeshare_tax_rate_pct == 32
    assert c.verified_on == date(2026, 11, 5) and "1120-H (2025)" in c.source


class TestWorksheet:
    def test_typical_hoa(self, books, homes):
        r = services.compute(services.get_worksheet(2025))
        assert r.lines[2] == 640_00  # interest
        assert r.lines[8] == 640_00 and r.lines[16] == 0
        assert r.lines[17] == 640_00 and r.lines[18] == 100_00 and r.lines[19] == 540_00
        assert r.lines[20] == 162_00  # 30% of 540
        gross, spend, res = r.tests
        assert gross.passed and gross.actual_pct == Decimal("93.4")  # 9025 of 9665
        assert spend.passed  # community events (300 of 7,800) are excluded by default: 96.2%
        assert res.passed  # all 10 homes residential

    def test_events_count_against_expenditure_test(self, books):
        ws = services.get_worksheet(2025)
        r = services.compute(ws)
        spend = r.tests[1]
        assert r.total_expenses_cents == 7800_00 and r.property_expenses_cents == 7500_00
        assert spend.actual_pct == Decimal("96.2") and spend.passed

    def test_specific_deduction_never_makes_negative(self, bank):
        txn(bank, 1000_00, "4000")
        txn(bank, 40_00, "4100")
        r = services.compute(services.get_worksheet(2025))
        assert r.lines[18] == 40_00 and r.lines[19] == 0 and r.lines[20] == 0

    def test_directly_connected_expense_and_line_mapping(self, bank):
        rent = Account.objects.create(
            code="4300", name="Clubhouse rentals", type="income", tax_class="nonexempt_income"
        )
        cleaning = Account.objects.create(
            code="5960", name="Clubhouse cleaning", type="expense", tax_class="nonexempt_expense"
        )
        txn(bank, 9000_00, "4000")
        txn(bank, 2000_00, "4300")
        txn(bank, -500_00, "5960")
        ws = services.get_worksheet(2025)
        r = services.compute(ws)
        assert r.lines[3] == 2000_00 and r.lines[15] == 500_00
        ws.line_map = {str(cleaning.pk): 10, str(rent.pk): 7}
        r = services.compute(ws)
        assert r.lines[7] == 2000_00 and r.lines[10] == 500_00 and r.lines[3] == 0
        assert r.lines[19] == 2000_00 - 500_00 - 100_00
        assert r.lines[20] == 420_00

    def test_unclassified_income_counts_as_taxable_and_warns(self, bank):
        txn(bank, 9000_00, "4000")
        txn(bank, 200_00, "4200")  # "Other income", tax class "other"
        r = services.compute(services.get_worksheet(2025))
        assert r.lines[7] == 200_00
        assert any("aren't tagged" in w for w in r.warnings)

    def test_adjustments_and_timeshare_rate(self, bank):
        txn(bank, 9000_00, "4000")
        txn(bank, 1000_00, "4100")
        ws = services.get_worksheet(2025)
        ws.adjustments = {
            "5": {"cents": 500_00, "note": "Sold equipment"},
            "14": {"cents": 200_00, "note": ""},
        }
        ws.association_type = Worksheet.AssociationType.TIMESHARE
        r = services.compute(ws)
        assert r.lines[5] == 500_00 and r.lines[14] == 200_00
        assert r.lines[19] == 1000_00 + 500_00 - 200_00 - 100_00
        assert r.lines[20] == 384_00  # 32% of 1,200

    def test_gross_income_test_fails(self, bank):
        txn(bank, 1000_00, "4000")
        txn(bank, 1000_00, "4100")
        r = services.compute(services.get_worksheet(2025))
        assert r.tests[0].actual_pct == Decimal("50.0") and not r.tests[0].passed and not r.qualifies

    def test_residential_test_uses_lot_counts(self, bank):
        txn(bank, 1000_00, "4000")
        ws = services.get_worksheet(2025)
        ws.total_lots, ws.residential_lots = 20, 16
        r = services.compute(ws)
        assert r.tests[2].actual_pct == Decimal("80.0") and not r.tests[2].passed

    def test_needs_private_benefit_confirmation(self, books, homes):
        ws = services.get_worksheet(2025)
        assert not services.compute(ws).qualifies
        ws.no_private_benefit = True
        assert services.compute(ws).qualifies

    def test_falls_back_to_previous_year_rules_with_warning(self, bank):
        txn(bank, 1000_00, "4000", on=date(2026, 3, 1))
        r = services.compute(services.get_worksheet(2026))
        assert r.config.year == 2025 and not r.config_exact
        assert any("No 1120-H rules are entered for 2026" in w for w in r.warnings)
        assert any("isn't over yet" in w for w in r.warnings)

    def test_configured_rates_are_used(self, bank):
        TaxYearConfig.objects.filter(year=2025).update(tax_rate_pct=Decimal("21"), specific_deduction_cents=0)
        txn(bank, 1000_00, "4100")
        r = services.compute(services.get_worksheet(2025))
        assert r.lines[20] == 210_00

    def test_uncategorized_warning_and_default_lots(self, bank):
        for n in range(4):
            Home.objects.create(address=f"{n} Elm")
        txn(bank, -10_00, None)
        ws = services.get_worksheet(2025)
        assert ws.total_lots == 4
        assert any("aren't categorized" in w for w in services.compute(ws).warnings)

    def test_due_date_skips_weekend(self):
        assert services.due_date(2025) == date(2026, 4, 15)  # a Wednesday
        assert services.due_date(2028) == date(2029, 4, 16)  # the 15th is a Sunday


@pytest.fixture
def homes(db):
    return [Home.objects.create(address=f"{n} Oak Lane") for n in range(10)]


class TestViews:
    def test_pages_and_save(self, books, homes, board_user, login):
        client = login(board_user)
        assert b"Tax year 2025" in client.get(reverse("tax:index")).content
        page = client.get(reverse("tax:worksheet", args=[2025])).content.decode()
        assert "Draft worksheet, not tax advice; review before filing." in page
        assert "$162.00" in page
        events = acct("5800")
        response = client.post(
            reverse("tax:worksheet", args=[2025]),
            {
                "association_type": "residential",
                "total_lots": 10,
                "residential_lots": 10,
                "tax_exempt_interest_cents": "0",
                "no_private_benefit": "on",
                f"line_{acct('4100').pk}": "7",
                "nonproperty": [str(events.pk)],
                "adj_14_cents": "100",
                "adj_14_note": "Mower",
            },
        )
        assert response.status_code == 302
        ws = Worksheet.objects.get(year=2025)
        assert ws.no_private_benefit and ws.line_map == {str(acct("4100").pk): 7}
        assert ws.adjustments == {"14": {"cents": 100_00, "note": "Mower"}}
        r = services.compute(ws)
        assert r.lines[7] == 640_00 and r.lines[14] == 100_00 and r.qualifies
        html = client.get(reverse("tax:worksheet_print", args=[2025])).content.decode()
        assert "Draft worksheet, not tax advice; review before filing." in html and "Mower" in html
        pdf = client.get(reverse("tax:worksheet_print", args=[2025]), {"format": "pdf"})
        assert pdf.content.startswith(b"%PDF")

    def test_bad_adjustment_is_an_error(self, books, board_user, login):
        client = login(board_user)
        response = client.post(
            reverse("tax:worksheet", args=[2025]),
            {"association_type": "residential", "tax_exempt_interest_cents": "0", "adj_7_cents": "lots"},
            follow=True,
        )
        assert b"isn&#x27;t an amount" in response.content
        assert not Worksheet.objects.exists()

    def test_future_year_404(self, board_user, login):
        assert login(board_user).get(reverse("tax:worksheet", args=[2999])).status_code == 404

    def test_rules_add_next_year_requires_confirmation(self, board_user, login):
        client = login(board_user)
        page = client.get(reverse("tax:rule_create")).content.decode()
        assert 'value="2026"' in page
        data = {
            "year": 2026,
            "gross_income_test_pct": "60",
            "expenditure_test_pct": "90",
            "residential_test_pct": "85",
            "specific_deduction_cents": "100",
            "tax_rate_pct": "30",
            "timeshare_tax_rate_pct": "32",
            "source": "IRS Instructions for Form 1120-H (2026)",
        }
        client.post(reverse("tax:rule_create"), data)
        assert not TaxYearConfig.objects.filter(year=2026).exists()
        client.post(reverse("tax:rule_create"), data | {"confirm": "on"})
        c = TaxYearConfig.objects.get(year=2026)
        assert c.verified_by == board_user and c.verified_on is not None

    def test_permissions(self, make_user, login, resident, client, books):
        sec = make_user("sec@example.com", "pw-long-enough-123")
        BoardPosition.objects.create(user=sec, title=BoardPosition.Title.SECRETARY)
        login(sec)
        assert client.get(reverse("tax:worksheet", args=[2025])).status_code == 200
        assert (
            client.post(reverse("tax:worksheet", args=[2025]), {"association_type": "condo"}).status_code
            == 404
        )
        assert client.get(reverse("tax:rule_create")).status_code == 403
        login(resident)
        for url in (reverse("tax:index"), reverse("tax:worksheet", args=[2025]), reverse("tax:rules")):
            assert client.get(url).status_code == 403
