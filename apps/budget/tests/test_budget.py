from datetime import date
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.accounts.models import BoardPosition
from apps.budget import calc, services
from apps.budget.models import (
    HistoricalAmount,
    HistoricalImport,
    HistoricalMapping,
    OneTimeItem,
    ReserveComponent,
    Scenario,
    ScenarioLine,
)
from apps.community.models import Home
from apps.dues.models import DuesSettings
from apps.finance.models import Account, BankAccount, Transaction

pytestmark = pytest.mark.django_db

HISTORY = {2022: 10000_00, 2023: 11000_00, 2024: 12000_00}


# ------------------------------------------------------------------ projection methods


class TestProjection:
    def p(self, method, year=2026, **kw):
        kw.setdefault("base_year", 2024)
        kw.setdefault("fiscal_year", 2026)
        return calc.project(method, HISTORY, year=year, **kw)

    def test_same(self):
        assert self.p(calc.SAME) == 12000_00
        assert self.p(calc.SAME, year=2028) == 12000_00

    def test_inflation_compounds_from_base_year(self):
        # 2024 -> 2026 is two years of 5%: 12000 * 1.05^2 = 13230
        assert self.p(calc.INFLATION, inflation_pct="5") == 13230_00
        assert self.p(calc.INFLATION, inflation_pct="0") == 12000_00

    def test_average_of_last_n_years(self):
        assert self.p(calc.AVERAGE, n_years=3) == 11000_00
        assert self.p(calc.AVERAGE, n_years=2) == 11500_00
        assert self.p(calc.AVERAGE, n_years=10) == 11000_00  # only the years we have

    def test_average_ignores_years_after_base(self):
        assert self.p(calc.AVERAGE, base_year=2023, n_years=2) == 10500_00

    def test_linear_trend(self):
        # +1000/yr: 2026 -> 14000, 2027 -> 15000
        assert self.p(calc.TREND, n_years=3) == 14000_00
        assert self.p(calc.TREND, year=2027, n_years=3) == 15000_00

    def test_trend_never_negative_and_single_point(self):
        falling = {2023: 3000_00, 2024: 1000_00}
        assert calc.project(calc.TREND, falling, base_year=2024, fiscal_year=2026, year=2026, n_years=2) == 0
        assert calc.project(calc.TREND, {2024: 500}, base_year=2024, fiscal_year=2025, year=2025) == 500

    def test_manual_grows_after_fiscal_year(self):
        assert self.p(calc.MANUAL, manual_cents=5000_00, inflation_pct="10") == 5000_00
        assert self.p(calc.MANUAL, year=2027, manual_cents=5000_00, inflation_pct="10") == 5500_00

    def test_exclude_and_no_history(self):
        assert self.p(calc.EXCLUDE) == 0
        assert calc.project(calc.AVERAGE, {}, base_year=2024, fiscal_year=2025, year=2025) == 0
        assert calc.project(calc.SAME, {}, base_year=2024, fiscal_year=2025, year=2025) == 0

    def test_unknown_method(self):
        with pytest.raises(ValueError):
            self.p("magic")


# ------------------------------------------------------------------ reserves


def fence(**kw):
    defaults = {
        "name": "Fence",
        "cost_cents": 20000_00,
        "useful_life": 20,
        "remaining_life": 5,
        "as_of_year": 2026,
        "annual_contribution_cents": 1000_00,
    }
    return calc.Component(**(defaults | kw))


class TestReserves:
    def test_fully_funded_is_used_up_share(self):
        # 15 of 20 years used: 75% of 20,000
        assert fence().fully_funded_cents() == 15000_00
        # two years later 17 of 20 used, cost inflated 10%/yr: 24,200 * 17/20
        assert fence().fully_funded_cents(2028, "10") == 20570_00
        assert fence(remaining_life=0).fully_funded_cents() == 20000_00
        assert fence(remaining_life=20).fully_funded_cents() == 0

    def test_straight_line_rounds_up(self):
        assert fence().straight_line_cents == 1000_00
        assert fence(cost_cents=1000, useful_life=3).straight_line_cents == 334

    def test_percent_funded(self):
        assert calc.percent_funded(7500_00, [fence()]) == Decimal("50.0")
        assert calc.percent_funded(100, []) is None

    def test_projection_replaces_and_recurs_with_inflation(self):
        sign = fence(
            name="Sign", cost_cents=1000_00, useful_life=2, remaining_life=1, annual_contribution_cents=0
        )
        rows = calc.reserve_projection(5000_00, [fence(), sign], 2026, 6, "0")
        by_year = {r.year: r for r in rows}
        assert [n for n, _ in by_year[2027].replacements] == ["Sign"]
        assert [n for n, _ in by_year[2029].replacements] == ["Sign"]
        assert [n for n, _ in by_year[2031].replacements] == ["Fence", "Sign"]
        assert by_year[2026].end_cents == 6000_00  # +1,000 contribution, nothing replaced
        assert by_year[2027].end_cents == 6000_00  # +1,000 - 1,000 sign
        assert by_year[2031].short  # the 20,000 fence empties the fund
        inflated = calc.reserve_projection(0, [sign], 2026, 2, "10", contributions={2026: 0, 2027: 0})
        assert inflated[1].spending_cents == 1100_00

    def test_projection_contribution_override_and_extra(self):
        rows = calc.reserve_projection(
            0, [fence()], 2026, 1, contributions={2026: 250_00}, extra={2026: 100_00}
        )
        assert rows[0].end_cents == 150_00


# ------------------------------------------------------------------ dues calculator


def dues(**kw):
    args = {
        "opex_cents": 40000_00,
        "reserve_cents": 10000_00,
        "contingency_pct": "5",
        "other_income_cents": 2000_00,
        "collection_rate_pct": "100",
        "homes": 100,
        "periods_per_year": 1,
        "current_annual_cents": 450_00,
        "round_to_dollar": False,
    }
    return calc.dues_needed(**(args | kw))


class TestDuesCalculator:
    def test_formula(self):
        d = dues()
        # 40,000 + 10,000 + 2,000 contingency - 2,000 other = 50,000 / 100 homes
        assert d.contingency_cents == 2000_00
        assert d.net_needed_cents == 50000_00 and d.gross_needed_cents == 50000_00
        assert d.annual_cents == 500_00 and d.per_period_cents == 500_00
        assert d.change_cents == 50_00 and d.change_pct == Decimal("11.1")

    def test_collection_rate_grosses_up(self):
        d = dues(collection_rate_pct="80")
        assert d.gross_needed_cents == 62500_00 and d.annual_cents == 625_00

    def test_periods_and_rounding_up(self):
        d = dues(periods_per_year=4)
        assert d.per_period_cents == 125_00 and d.annual_cents == 500_00
        d = dues(opex_cents=40000_01, contingency_pct="0", other_income_cents=0, reserve_cents=0, homes=3)
        assert d.per_period_cents == 1333334  # 4,000,001 / 3 rounded up to the cent
        d = dues(
            opex_cents=40000_01,
            contingency_pct="0",
            other_income_cents=0,
            reserve_cents=0,
            homes=3,
            round_to_dollar=True,
        )
        assert d.per_period_cents == 13334_00

    def test_other_income_can_cover_everything(self):
        assert dues(other_income_cents=10**9).annual_cents == 0

    def test_cap_flag_and_shortfall(self):
        d = dues(cap_pct="5", collection_rate_pct="90")
        # needed: 50,000 / 0.9 = 55,555.56 -> 555.56/home; cap: 450 * 1.05 = 472.50
        assert d.over_cap and d.capped_annual_cents == 472_50
        assert d.capped_shortfall_cents == round((555_56 - 472_50) * 100 * 0.9)
        assert not dues(cap_pct="50").over_cap
        assert not dues(cap_pct="5", current_annual_cents=0).over_cap  # nothing to cap against

    def test_cap_rounds_down_to_dollar(self):
        d = dues(cap_pct="5", current_annual_cents=455_00, round_to_dollar=True)
        assert d.capped_per_period_cents == 477_00  # 477.75 rounded down

    def test_bad_inputs(self):
        with pytest.raises(ValueError):
            dues(homes=0)
        with pytest.raises(ValueError):
            dues(collection_rate_pct="0")

    def test_operating_projection_flags_low_years(self):
        rows = calc.operating_projection(
            start_cents=1000_00,
            years=[2026, 2027],
            dues_per_home={2026: 100_00, 2027: 100_00},
            homes=10,
            collection_rate_pct="90",
            other_income={2026: 0},
            expenses={2026: 1000_00, 2027: 1500_00},
            reserve_transfers={2026: 100_00, 2027: 100_00},
            min_balance_cents=500_00,
        )
        assert rows[0].dues_cents == 900_00 and rows[0].end_cents == 800_00 and not rows[0].below_minimum
        assert rows[1].end_cents == 100_00 and rows[1].below_minimum


# ------------------------------------------------------------------ actuals & scenarios


def acct(code):
    return Account.objects.get(code=code)


@pytest.fixture
def bank(db):
    return BankAccount.objects.create(
        name="Operating", opening_date=date(2024, 1, 1), opening_balance_cents=0
    )


def txn(bank, on, cents, code, n=[0]):  # noqa: B006 - counter for unique keys
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


def history_batch(rows):
    batch = HistoricalImport.objects.create(filename="old.csv", sha256="x")
    for year, code, cents in rows:
        HistoricalAmount.objects.create(batch=batch, year=year, account=acct(code), amount_cents=cents)
    return batch


class TestActuals:
    def test_bank_and_spreadsheet_years(self, bank):
        history_batch([(2023, "5000", 9000_00), (2023, "5200", 3000_00)])
        txn(bank, date(2023, 6, 1), -99_00, "5000")  # ignored: 2023 comes from the spreadsheet
        txn(bank, date(2024, 6, 1), -10000_00, "5000")
        txn(bank, date(2024, 7, 1), 250_00, "5000")  # refund
        txn(bank, date(2024, 7, 1), 50_00, "4100")
        txn(bank, date(2024, 7, 1), -5000_00, "9000")  # transfer
        txn(bank, date(2024, 7, 1), -77_00, None)  # uncategorized
        data = services.actuals()
        assert data.years == [2023, 2024]
        assert data.sources == {2023: "spreadsheet", 2024: "bank"}
        assert data.series(acct("5000").pk) == {2023: 9000_00, 2024: 9750_00}
        assert data.series(acct("5200").pk) == {2023: 3000_00, 2024: 0}
        assert data.series(acct("4100").pk) == {2023: 0, 2024: 50_00}
        assert acct("9000").pk not in data.by_account

    def test_current_year_is_partial(self, bank):
        from django.utils import timezone

        txn(bank, timezone.localdate(), -100, "5000")
        data = services.actuals()
        assert data.partial_year == timezone.localdate().year
        assert data.partial_year not in services.complete_years(data)


@pytest.fixture
def homes(db):
    return [Home.objects.create(address=f"{n} Oak Lane") for n in range(1, 11)]


def make_scenario(user=None, **kw):
    from apps.budget.forms import ScenarioForm

    data = {
        "name": "Proposed",
        "fiscal_year": 2026,
        "base_year": 2024,
        "inflation_pct": "0",
        "contingency_pct": "0",
        "collection_rate_pct": "100",
        "projection_years": 3,
        "future_increase_pct": "0",
        "starting_operating_cents": "1000.00",
        "min_operating_balance_cents": "500.00",
        "starting_reserve_cents": "0",
        "round_to_dollar": "",
    } | kw
    form = ScenarioForm(data)
    assert form.is_valid(), form.errors
    return services.create_scenario(form, user)


class TestScenario:
    def test_compute_end_to_end(self, bank, homes):
        history_batch(
            [
                (2024, "5000", 4000_00),
                (2024, "5200", 1000_00),
                (2024, "4100", 200_00),
                (2024, "6000", 9999_00),
            ]
        )
        settings = DuesSettings.load()
        settings.amount_cents = 450_00
        settings.save()
        ReserveComponent.objects.create(
            name="Sign",
            replacement_cost_cents=3000_00,
            useful_life_years=10,
            remaining_life_years=10,
            as_of_year=2026,
        )
        s = make_scenario()
        OneTimeItem.objects.create(scenario=s, year=2026, description="Legal", amount_cents=800_00)
        OneTimeItem.objects.create(
            scenario=s, year=2027, description="Repave", amount_cents=500_00, from_reserve=True
        )
        assert s.lines.get(account__code="6000").method == "exclude"  # reserve spending
        assert not s.lines.filter(account__code="4000").exists()  # dues are the answer, not an input
        r = services.compute(s)
        d = r.dues
        # opex 4,000 + 1,000 + 800 legal; reserve 300 (straight line); other income 200
        assert d.opex_cents == 5800_00 and d.reserve_cents == 300_00 and d.other_income_cents == 200_00
        assert d.homes == 10 and d.annual_cents == 590_00
        assert r.cash[0].end_cents == 1000_00 + 5900_00 + 200_00 - 5800_00 - 300_00
        assert r.cash[1].below_minimum is False
        assert r.reserve[1].spending_cents == 500_00 and r.reserve[1].end_cents == 100_00
        summary = services.summary(r)
        assert summary["per_period_cents"] == 590_00
        names = [x["name"] for x in summary["expense"]]
        assert names[0] == "Landscaping & grounds" and "Insurance" in names
        assert "Reserve projects" not in names  # excluded lines aren't shown

    def test_homes_override_and_lines_for_new_category(self, homes):
        s = make_scenario(homes_count="40")
        Account.objects.create(code="5950", name="Pool", type="expense")
        r = services.compute(s)
        assert r.homes == 40
        assert s.lines.filter(account__code="5950").exists()

    def test_copy_keeps_lines_and_items(self, homes):
        s = make_scenario()
        line = s.lines.get(account__code="5000")
        line.method, line.manual_cents = "manual", 1234_00
        line.save()
        OneTimeItem.objects.create(scenario=s, year=2026, description="Legal", amount_cents=1)
        from apps.budget.forms import ScenarioForm

        form = ScenarioForm({**ScenarioForm(instance=s).initial, "name": "Copy", "round_to_dollar": ""})
        form.data = {k: ("" if v is None else v) for k, v in form.data.items()}
        assert form.is_valid(), form.errors
        copy = services.create_scenario(form, None, copy_from=s)
        assert copy.lines.get(account__code="5000").manual_cents == 1234_00
        assert copy.one_time_items.count() == 1

    def test_adopt_updates_dues_and_is_unique(self, homes, board_user, login):
        history_batch([(2024, "5000", 5000_00)])
        a = make_scenario()
        b = make_scenario(name="Lean")
        client = login(board_user)
        assert client.get(reverse("budget:scenario_adopt", args=[a.pk])).status_code == 200
        client.post(reverse("budget:scenario_adopt", args=[a.pk]))
        assert DuesSettings.load().amount_cents == 500_00
        client.post(reverse("budget:scenario_adopt", args=[b.pk]))
        a.refresh_from_db()
        b.refresh_from_db()
        assert not a.is_adopted and b.is_adopted and b.adopted_per_period_cents == 500_00
        # Still compared with the dues before any budget was adopted, not with itself.
        assert services.compute(b).dues.current_annual_cents == 0
        assert b.replaced_per_period_cents == 0

    def test_publish_freezes_a_copy_for_residents(self, homes, board_user, resident, login, client):
        history_batch([(2024, "5000", 5000_00)])
        dues = DuesSettings.load()
        dues.amount_cents = 450_00
        dues.save()
        s = make_scenario(homes_count="10")
        login(board_user)
        client.post(reverse("budget:scenario_publish", args=[s.pk]))
        s.refresh_from_db()
        assert not s.is_published  # must be adopted first
        client.post(reverse("budget:scenario_adopt", args=[s.pk]))
        client.post(reverse("budget:scenario_publish", args=[s.pk]))
        s.refresh_from_db()
        assert s.published_snapshot["per_period_cents"] == 500_00
        assert s.published_snapshot["current_annual_cents"] == 450_00
        assert s.published_snapshot["change_cents"] == 50_00
        history_batch([(2024, "5000", 99999_00)])  # later changes don't alter what was published
        login(resident)
        page = client.get(reverse("budget:published", args=[2026]))
        assert page.status_code == 200 and b"$500.00" in page.content
        assert client.get(reverse("budget:published", args=[2026]), {"format": "pdf"}).content.startswith(
            b"%PDF"
        )
        assert b"2026 budget" in client.get(reverse("budget:published_list")).content
        assert client.get(reverse("budget:scenario", args=[s.pk])).status_code == 403
        login(board_user)
        client.post(reverse("budget:scenario_publish", args=[s.pk]), {"action": "unpublish"})
        login(resident)
        assert client.get(reverse("budget:published", args=[2026])).status_code == 404


class TestPermissions:
    def test_secretary_drafts_but_cannot_adopt_or_publish(self, make_user, login, homes):
        sec = make_user("sec@example.com", "pw-long-enough-123")
        BoardPosition.objects.create(user=sec, title=BoardPosition.Title.SECRETARY)
        client = login(sec)
        assert client.get(reverse("budget:scenario_create")).status_code == 200
        s = make_scenario()
        assert client.get(reverse("budget:scenario", args=[s.pk])).status_code == 200
        assert client.post(reverse("budget:scenario_adopt", args=[s.pk])).status_code == 403
        assert client.post(reverse("budget:scenario_publish", args=[s.pk])).status_code == 403
        assert client.get(reverse("budget:history_upload")).status_code == 403

    def test_residents_and_anonymous_blocked(self, resident, login, client):
        assert client.get(reverse("budget:home")).status_code == 302
        login(resident)
        for name in ("budget:home", "budget:lookback", "budget:reserves", "budget:compare", "budget:history"):
            assert client.get(reverse(name)).status_code == 403


class TestViews:
    def test_pages_render(self, bank, homes, board_user, login):
        history_batch([(2023, "5000", 4000_00), (2024, "5000", 4500_00)])
        txn(bank, date(2025, 3, 1), -4800_00, "5000")
        ReserveComponent.objects.create(
            name="Fence",
            replacement_cost_cents=20000_00,
            useful_life_years=20,
            remaining_life_years=1,
            as_of_year=2026,
        )
        s = make_scenario(base_year=2025)
        make_scenario(name="Other", base_year=2025)
        client = login(board_user)
        look = client.get(reverse("budget:lookback")).content.decode()
        assert "$4,800.00" in look and "spreadsheet" in look and "+6.7%" in look
        assert f"/board/finance/transactions/?year=2025&amp;account={acct('5000').pk}" in look
        assert f"/board/budget/history/?year=2024&amp;account={acct('5000').pk}" in look
        page = client.get(reverse("budget:scenario", args=[s.pk])).content.decode()
        assert "Fence" in page and "Dues must bring in" in page
        assert b"Other" in client.get(reverse("budget:compare"), {"year": 2026}).content
        assert client.get(reverse("budget:home")).status_code == 200
        assert b"Fence" in client.get(reverse("budget:reserves")).content
        assert b"DRAFT" in client.get(reverse("budget:scenario_print", args=[s.pk])).content
        pdf = client.get(reverse("budget:scenario_print", args=[s.pk]), {"format": "pdf"})
        assert pdf.content.startswith(b"%PDF")

    def test_edit_lines_and_one_time_items(self, homes, board_user, login):
        s = make_scenario()
        line = s.lines.get(account__code="5000")
        client = login(board_user)
        client.post(
            reverse("budget:scenario_lines", args=[s.pk]),
            {f"method_{line.pk}": "manual", f"manual_{line.pk}": "$1,500.00", f"years_{line.pk}": "4"},
        )
        line.refresh_from_db()
        assert (line.method, line.manual_cents, line.years) == ("manual", 1500_00, 4)
        client.post(
            reverse("budget:scenario_lines", args=[s.pk]),
            {f"method_{line.pk}": "manual", f"manual_{line.pk}": ""},
        )
        line.refresh_from_db()
        assert line.manual_cents is None  # and an error message asks for the amount
        client.post(
            reverse("budget:one_time_item", args=[s.pk]),
            {"year": 2027, "description": "Survey", "amount_cents": "250"},
        )
        item = s.one_time_items.get()
        assert item.amount_cents == 250_00
        client.post(
            reverse("budget:one_time_item", args=[s.pk]),
            {"year": 2040, "description": "x", "amount_cents": "1"},
        )
        assert s.one_time_items.count() == 1  # outside the projection years
        client.post(reverse("budget:one_time_item", args=[s.pk]), {"delete": item.pk})
        assert not s.one_time_items.exists()

    def test_scenario_validation(self):
        from apps.budget.forms import ScenarioForm

        form = ScenarioForm({"name": "x", "fiscal_year": 2026, "base_year": 2026})
        assert not form.is_valid() and "base_year" in form.errors

    def test_component_validation(self, board_user, login):
        client = login(board_user)
        response = client.post(
            reverse("budget:component_create"),
            {
                "name": "Wall",
                "replacement_cost_cents": "5000",
                "useful_life_years": 10,
                "remaining_life_years": 12,
                "as_of_year": 2026,
            },
        )
        assert b"can&#x27;t be longer" in response.content
        client.post(
            reverse("budget:component_create"),
            {
                "name": "Wall",
                "replacement_cost_cents": "5000",
                "useful_life_years": 10,
                "remaining_life_years": 4,
                "as_of_year": 2026,
                "is_active": "on",
            },
        )
        wall = ReserveComponent.objects.get()
        assert wall.as_calc().annual_contribution_cents == 500_00

    def test_delete_rules(self, homes, board_user, login):
        s = make_scenario()
        s.is_adopted = True
        s.save()
        client = login(board_user)
        client.post(reverse("budget:scenario_delete", args=[s.pk]))
        assert Scenario.objects.filter(pk=s.pk).exists()


# ------------------------------------------------------------------ historical import


CSV = b"""Old HOA books export
Fiscal Year,Line Item,Actual,Memo
2023,Lawn,"9,000.00",Greenscape
2023,Insurance,3000,
2024,Lawn,9500,
2024,Postage,40,stamps
2024,Lawn,oops,
"""


class TestHistoryImport:
    def start(self, client, content=CSV, name="books.csv"):
        return client.post(reverse("budget:history_upload"), {"file": SimpleUploadedFile(name, content)})

    def columns(self, **extra):
        return {
            "col_year": "Fiscal Year",
            "col_category": "Line Item",
            "col_amount": "Actual",
            "col_description": "Memo",
            "amount_sign": "positive",
        } | extra

    def test_full_flow_with_category_mapping_saved_for_reimport(self, board_user, login):
        client = login(board_user)
        assert self.start(client).url == reverse("budget:history_map")
        page = client.get(reverse("budget:history_map"))
        assert page.status_code == 200
        html = client.post(reverse("budget:history_map"), self.columns()).content.decode()
        assert "Match categories" in html and "Lawn" in html and "1 row can" in html
        insurance = acct("5200")
        # categories are sorted: Insurance (0), Lawn (1), Postage (2)
        data = self.columns(
            cat_0=insurance.pk, cat_1=acct("5000").pk, cat_2="", save_as="Old books", confirm="1"
        )
        response = client.post(reverse("budget:history_map"), data)
        assert response.status_code == 302
        rows = HistoricalAmount.objects.order_by("year", "account__code")
        assert [(r.year, r.account.code, r.amount_cents) for r in rows] == [
            (2023, "5000", 9000_00),
            (2023, "5200", 3000_00),
            (2024, "5000", 9500_00),
        ]
        mapping = HistoricalMapping.objects.get(name="Old books")
        assert mapping.category_map == {"Insurance": insurance.pk, "Lawn": acct("5000").pk}
        assert mapping.columns["category"] == "Line Item"

        # Re-import: the saved mapping fills in columns and categories; same file needs confirmation.
        self.start(client)
        html = client.get(reverse("budget:history_map"), {"mapping": mapping.pk}).content.decode()
        assert 'value="Line Item" selected' in html
        html = client.post(
            reverse("budget:history_map") + f"?mapping={mapping.pk}", self.columns()
        ).content.decode()
        assert f'<option value="{insurance.pk}" selected' in html and "already imported" not in html
        dup = client.post(reverse("budget:history_map") + f"?mapping={mapping.pk}", self.columns(confirm="1"))
        assert b"already imported" in dup.content and HistoricalImport.objects.count() == 1
        client.post(
            reverse("budget:history_map") + f"?mapping={mapping.pk}",
            self.columns(confirm="1", allow_duplicate="1"),
        )
        assert HistoricalImport.objects.count() == 2

    def test_bank_style_signs_and_default_year(self, board_user, login):
        client = login(board_user)
        self.start(client, b"Category,Amount\nLandscaping & grounds,-1200\nInterest income,15\n")
        data = {
            "col_category": "Category",
            "col_amount": "Amount",
            "default_year": 2022,
            "amount_sign": "bank",
        }
        client.post(reverse("budget:history_map"), data | {"confirm": "1"})
        got = {a.account.code: a.amount_cents for a in HistoricalAmount.objects.all()}
        assert got == {"5000": 1200_00, "4100": 15_00}  # names matched to categories automatically

    def test_needs_a_year(self, board_user, login):
        client = login(board_user)
        self.start(client, b"Category,Amount\nLawn,1\n")
        html = client.post(
            reverse("budget:history_map"),
            {"col_category": "Category", "col_amount": "Amount", "amount_sign": "positive"},
        )
        assert b"enter the year" in html.content

    def test_remove_import(self, board_user, login):
        batch = history_batch([(2023, "5000", 1)])
        login(board_user).post(reverse("budget:history"), {"delete_batch": batch.pk})
        assert not HistoricalAmount.objects.exists()

    def test_expired_upload(self, board_user, login):
        response = login(board_user).get(reverse("budget:history_map"))
        assert response.url == reverse("budget:history_upload")


def test_scenario_line_methods_match_calc():
    assert {m for m, _ in ScenarioLine.Method.choices} == {
        calc.SAME,
        calc.INFLATION,
        calc.AVERAGE,
        calc.TREND,
        calc.MANUAL,
        calc.EXCLUDE,
    }
