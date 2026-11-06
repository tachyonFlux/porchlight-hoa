"""Budget planning: actuals by category/year, scenario projections, adopting and publishing."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field

from django.db import transaction
from django.db.models import Sum
from django.db.models.functions import ExtractYear
from django.utils import timezone

from apps.core import audit
from apps.finance import parsers
from apps.finance.models import Account, BankAccount, Transaction

from . import calc
from .models import HistoricalAmount, HistoricalImport, ReserveComponent, Scenario, ScenarioLine

BANK = "bank"
SPREADSHEET = "spreadsheet"

# ------------------------------------------------------------------ actuals


@dataclass
class Actuals:
    years: list  # ascending
    sources: dict  # year -> BANK | SPREADSHEET
    by_account: dict  # account_id -> {year: cents}, every year present (0 if no activity)
    accounts: list  # income/expense Accounts with any activity, by code
    partial_year: int | None  # the current year when it has bank data (not complete yet)

    def series(self, account_id):
        return self.by_account.get(account_id) or dict.fromkeys(self.years, 0)


def actuals():
    """Income and spending per category per year.

    A year imported from a spreadsheet uses the spreadsheet (the board's own books for that
    year); other years use categorized bank transactions. Transfers are left out. Amounts are
    in each category's own direction: refunds reduce spending.
    """
    amounts = defaultdict(int)
    spreadsheet_years = set()
    for row in HistoricalAmount.objects.values("account_id", "year").annotate(t=Sum("amount_cents")):
        amounts[(row["account_id"], row["year"])] += row["t"]
        spreadsheet_years.add(row["year"])
    kinds = dict(Account.objects.values_list("id", "type"))
    bank_years = set()
    rows = (
        Transaction.objects.exclude(account__type=Account.Type.TRANSFER)
        .exclude(account__isnull=True)
        .annotate(y=ExtractYear("date"))
        .values("account_id", "y")
        .annotate(t=Sum("amount_cents"))
    )
    for row in rows:
        if row["y"] in spreadsheet_years:
            continue
        bank_years.add(row["y"])
        sign = -1 if kinds.get(row["account_id"]) == Account.Type.EXPENSE else 1
        amounts[(row["account_id"], row["y"])] += sign * row["t"]
    years = sorted(spreadsheet_years | bank_years)
    account_ids = {a for a, _ in amounts}
    accounts = list(
        Account.objects.filter(pk__in=account_ids, type__in=[Account.Type.INCOME, Account.Type.EXPENSE])
    )
    by_account = {a.pk: {y: amounts.get((a.pk, y), 0) for y in years} for a in accounts}
    this_year = timezone.localdate().year
    return Actuals(
        years=years,
        sources={y: SPREADSHEET if y in spreadsheet_years else BANK for y in years},
        by_account=by_account,
        accounts=accounts,
        partial_year=this_year if this_year in bank_years else None,
    )


def complete_years(data):
    return [y for y in data.years if y != data.partial_year]


# ------------------------------------------------------------------ scenarios


def dues_account():
    """The category dues are deposited to (they're what the budget solves for, not an input)."""
    return Account.objects.filter(type=Account.Type.INCOME, code="4000").first() or (
        Account.objects.filter(type=Account.Type.INCOME, tax_class=Account.TaxClass.EXEMPT_INCOME)
        .order_by("code")
        .first()
    )


def plannable_accounts():
    dues = dues_account()
    qs = Account.objects.filter(is_active=True, type__in=[Account.Type.INCOME, Account.Type.EXPENSE])
    return qs.exclude(pk=dues.pk) if dues else qs


def default_method(account):
    # Reserve spending comes out of the reserve fund, which the reserve plan covers.
    if account.type == Account.Type.EXPENSE and "reserve" in account.name.lower():
        return ScenarioLine.Method.EXCLUDE
    return ScenarioLine.Method.INFLATION


def suggested_start(fiscal_year):
    """Defaults for a new scenario's starting balances: today's book balances."""
    from apps.finance.services import book_balance

    today = timezone.localdate()
    op = res = 0
    for bank in BankAccount.objects.filter(is_active=True):
        bal = book_balance(bank, today)
        if bank.is_reserve:
            res += bal
        else:
            op += bal
    return op, res


def default_base_year(fiscal_year):
    data = actuals()
    done = [y for y in complete_years(data) if y < fiscal_year]
    return done[-1] if done else fiscal_year - 1


@transaction.atomic
def create_scenario(form, user, copy_from=None):
    scenario = form.save(commit=False)
    scenario.created_by = user
    scenario.is_adopted = False
    scenario.published_snapshot = None
    scenario.save()
    old_lines = {line.account_id: line for line in copy_from.lines.all()} if copy_from else {}
    for account in plannable_accounts():
        old = old_lines.get(account.pk)
        ScenarioLine.objects.create(
            scenario=scenario,
            account=account,
            method=old.method if old else default_method(account),
            years=old.years if old else 3,
            manual_cents=old.manual_cents if old else None,
            note=old.note if old else "",
        )
    if copy_from:
        for item in copy_from.one_time_items.all():
            item.pk = None
            item.scenario = scenario
            item.save()
    return scenario


def ensure_lines(scenario):
    """Add lines for categories created after the scenario was."""
    have = set(scenario.lines.values_list("account_id", flat=True))
    for account in plannable_accounts().exclude(pk__in=have):
        ScenarioLine.objects.create(scenario=scenario, account=account, method=default_method(account))


@dataclass
class LineResult:
    line: ScenarioLine
    history: dict  # year -> cents (complete years up to the base year)
    projected: dict  # year -> cents

    @property
    def account(self):
        return self.line.account

    @property
    def budget_cents(self):
        return self.projected[min(self.projected)]

    @property
    def last_actual(self):
        return self.history.get(self.line.scenario.base_year)

    @property
    def change_pct(self):
        last = self.last_actual
        if not last:
            return None
        return round((self.budget_cents - last) * 100 / last, 1)


@dataclass
class ScenarioResult:
    scenario: Scenario
    years: list
    history_years: list
    income: list
    expense: list
    opex: dict
    other_income: dict
    one_time_op_expense: dict
    one_time_op_income: dict
    reserve_extra: dict
    contributions: dict
    components: list
    reserve: list
    dues: calc.DuesResult | None
    dues_error: str
    cash: list
    homes: int
    current_per_period_cents: int
    periods_per_year: int
    percent_funded: object = None
    fully_funded_cents: int = 0
    warnings: list = field(default_factory=list)

    @property
    def fiscal_year(self):
        return self.scenario.fiscal_year

    @property
    def lowest_cash(self):
        return min(self.cash, key=lambda r: r.end_cents) if self.cash else None

    @property
    def lowest_reserve(self):
        return min(self.reserve, key=lambda r: r.end_cents) if self.reserve else None

    @property
    def cash_flags(self):
        return [r for r in self.cash if r.below_minimum]

    @property
    def reserve_flags(self):
        return [r for r in self.reserve if r.short]


def homes_for(scenario):
    from apps.community.models import Home

    return scenario.homes_count or Home.objects.filter(is_active=True).count()


def compute(scenario, data=None):
    from apps.dues.models import DuesSettings

    data = data or actuals()
    ensure_lines(scenario)
    years = list(range(scenario.fiscal_year, scenario.fiscal_year + scenario.projection_years))
    history_years = [y for y in complete_years(data) if y <= scenario.base_year]
    infl = scenario.inflation_pct
    income, expense = [], []
    opex = dict.fromkeys(years, 0)
    other_income = dict.fromkeys(years, 0)
    for line in scenario.lines.select_related("account").order_by("account__code"):
        series = data.series(line.account_id)
        history = {y: series.get(y, 0) for y in history_years}
        projected = {
            y: calc.project(
                line.method,
                history,
                base_year=scenario.base_year,
                fiscal_year=scenario.fiscal_year,
                year=y,
                inflation_pct=infl,
                n_years=line.years,
                manual_cents=line.manual_cents,
            )
            for y in years
        }
        result = LineResult(line, history, projected)
        target, totals = (
            (income, other_income) if line.account.type == Account.Type.INCOME else (expense, opex)
        )
        target.append(result)
        for y in years:
            totals[y] += projected[y]

    op_exp, op_inc, reserve_extra = (dict.fromkeys(years, 0) for _ in range(3))
    for item in scenario.one_time_items.all():
        if item.year not in op_exp:
            continue
        if item.from_reserve:
            reserve_extra[item.year] += -item.amount_cents if item.is_income else item.amount_cents
        elif item.is_income:
            op_inc[item.year] += item.amount_cents
        else:
            op_exp[item.year] += item.amount_cents

    components = [c.as_calc() for c in ReserveComponent.objects.filter(is_active=True)]
    yearly = (
        scenario.reserve_contribution_cents
        if scenario.reserve_contribution_cents is not None
        else sum(c.annual_contribution_cents for c in components)
    )
    contributions = dict.fromkeys(years, yearly)
    reserve = calc.reserve_projection(
        scenario.starting_reserve_cents,
        components,
        scenario.fiscal_year,
        len(years),
        infl,
        contributions,
        reserve_extra,
    )

    settings = DuesSettings.load()
    homes = homes_for(scenario)
    fy = scenario.fiscal_year
    current_per_period = settings.amount_cents
    if scenario.is_adopted and scenario.replaced_per_period_cents is not None:
        current_per_period = scenario.replaced_per_period_cents
    dues, dues_error = None, ""
    try:
        dues = calc.dues_needed(
            opex_cents=opex[fy] + op_exp[fy],
            reserve_cents=contributions[fy],
            contingency_pct=scenario.contingency_pct,
            other_income_cents=other_income[fy] + op_inc[fy],
            collection_rate_pct=scenario.collection_rate_pct,
            homes=homes,
            periods_per_year=settings.periods_per_year,
            current_annual_cents=current_per_period * settings.periods_per_year,
            cap_pct=scenario.increase_cap_pct,
            round_to_dollar=scenario.round_to_dollar,
        )
    except ValueError as exc:
        dues_error = str(exc)

    cash = []
    if dues:
        per_home = {}
        amount = dues.annual_cents
        for y in years:
            per_home[y] = amount
            amount = calc.grow(amount, scenario.future_increase_pct, 1)
        cash = calc.operating_projection(
            start_cents=scenario.starting_operating_cents,
            years=years,
            dues_per_home=per_home,
            homes=homes,
            collection_rate_pct=scenario.collection_rate_pct,
            other_income={y: other_income[y] + op_inc[y] for y in years},
            expenses={y: opex[y] + op_exp[y] for y in years},
            reserve_transfers=contributions,
            min_balance_cents=scenario.min_operating_balance_cents,
        )

    result = ScenarioResult(
        scenario=scenario,
        years=years,
        history_years=history_years[-5:],
        income=income,
        expense=expense,
        opex=opex,
        other_income=other_income,
        one_time_op_expense=op_exp,
        one_time_op_income=op_inc,
        reserve_extra=reserve_extra,
        contributions=contributions,
        components=components,
        reserve=reserve,
        dues=dues,
        dues_error=dues_error,
        cash=cash,
        homes=homes,
        current_per_period_cents=current_per_period,
        periods_per_year=settings.periods_per_year,
        percent_funded=calc.percent_funded(scenario.starting_reserve_cents, components, fy, infl),
        fully_funded_cents=calc.fully_funded_total(components, fy, infl),
    )
    if not history_years:
        result.warnings.append(
            "There are no complete years of actuals yet. Import a spreadsheet of past years, or "
            "use 'Enter an amount' for each line."
        )
    elif scenario.base_year not in history_years:
        result.warnings.append(f"There are no actuals for the base year {scenario.base_year}.")
    if data.partial_year and data.partial_year <= scenario.base_year:
        result.warnings.append(f"{data.partial_year} isn't over yet, so its actuals are incomplete.")
    if dues and dues.over_cap:
        result.warnings.append(
            f"The increase ({dues.change_pct}%) is more than the {_pct(dues.cap_pct)}% yearly cap."
        )
    return result


def _pct(value):
    text = f"{value:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def summary(result):
    """A plain-data summary of a scenario: used for print/PDF and frozen when published."""
    d = result.dues
    fy = result.fiscal_year

    def lines(items):
        return [
            {"code": r.account.code, "name": r.account.name, "cents": r.budget_cents, "last": r.last_actual}
            for r in items
            if r.line.method != calc.EXCLUDE and (r.budget_cents or r.last_actual)
        ]

    one_time = [
        {
            "description": i.description,
            "cents": i.amount_cents,
            "income": i.is_income,
            "reserve": i.from_reserve,
        }
        for i in result.scenario.one_time_items.filter(year=fy)
    ]
    return {
        "name": result.scenario.name,
        "fiscal_year": fy,
        "base_year": result.scenario.base_year,
        "income": lines(result.income),
        "expense": lines(result.expense),
        "one_time": one_time,
        "other_income_cents": result.other_income[fy] + result.one_time_op_income[fy],
        "opex_cents": result.opex[fy] + result.one_time_op_expense[fy],
        "reserve_contribution_cents": result.contributions[fy],
        "contingency_pct": str(result.scenario.contingency_pct),
        "contingency_cents": d.contingency_cents if d else 0,
        "collection_rate_pct": str(result.scenario.collection_rate_pct),
        "homes": result.homes,
        "per_period_cents": d.per_period_cents if d else None,
        "annual_cents": d.annual_cents if d else None,
        "periods_per_year": result.periods_per_year,
        "current_annual_cents": d.current_annual_cents if d else None,
        "change_cents": d.change_cents if d else None,
        "change_pct": str(d.change_pct) if d and d.change_pct is not None else None,
        "reserve_start_cents": result.scenario.starting_reserve_cents,
        "fully_funded_cents": result.fully_funded_cents,
        "percent_funded": str(result.percent_funded) if result.percent_funded is not None else None,
        "reserve": [
            {
                "year": r.year,
                "contribution": r.contribution_cents,
                "spending": r.spending_cents,
                "end": r.end_cents,
                "items": [name for name, _ in r.replacements],
            }
            for r in result.reserve
        ],
        "notes": result.scenario.notes,
    }


@transaction.atomic
def adopt(scenario, user, request=None):
    """Make this the budget for its year and set the dues for billing periods created from now on."""
    from apps.dues.models import DuesSettings

    result = compute(scenario)
    if not result.dues:
        raise ValueError(result.dues_error or "Dues couldn't be calculated.")
    Scenario.objects.filter(fiscal_year=scenario.fiscal_year, is_adopted=True).exclude(pk=scenario.pk).update(
        is_adopted=False
    )
    settings = DuesSettings.load()
    old = settings.amount_cents
    settings.amount_cents = result.dues.per_period_cents
    settings.save()
    scenario.is_adopted = True
    scenario.adopted_at = timezone.now()
    scenario.adopted_by = user
    scenario.adopted_per_period_cents = result.dues.per_period_cents
    if scenario.replaced_per_period_cents is None:
        # Re-adopting keeps the original "before" amount; others adopted this year keep theirs.
        previous = (
            Scenario.objects.filter(fiscal_year=scenario.fiscal_year, replaced_per_period_cents__isnull=False)
            .exclude(pk=scenario.pk)
            .values_list("replaced_per_period_cents", flat=True)
            .first()
        )
        scenario.replaced_per_period_cents = previous if previous is not None else old
    scenario.save()
    audit.record(
        request or user,
        "budget.adopted",
        scenario,
        {"dues_from": old, "dues_to": result.dues.per_period_cents},
    )
    return result


@transaction.atomic
def publish(scenario, user, request=None):
    Scenario.objects.filter(fiscal_year=scenario.fiscal_year, published_snapshot__isnull=False).exclude(
        pk=scenario.pk
    ).update(published_snapshot=None, published_at=None)
    scenario.published_snapshot = summary(compute(scenario))
    scenario.published_at = timezone.now()
    scenario.save(update_fields=["published_snapshot", "published_at", "updated_at"])
    audit.record(request or user, "budget.published", scenario)


def unpublish(scenario, user, request=None):
    scenario.published_snapshot = None
    scenario.published_at = None
    scenario.save(update_fields=["published_snapshot", "published_at", "updated_at"])
    audit.record(request or user, "budget.unpublished", scenario)


# ------------------------------------------------------------------ historical spreadsheet import

HISTORY_FIELDS = {
    "year": "Year",
    "date": "Date (if there's no year column)",
    "category": "Category",
    "amount": "Amount",
    "description": "Description (optional)",
}


class HistoryError(Exception):
    pass


def read_history_csv(text):
    """Return (headers, records). The header is the first row naming a category column."""
    import csv
    import io

    rows = list(csv.reader(io.StringIO(text)))
    for i, row in enumerate(rows[:20]):
        lowered = [c.strip().lower() for c in row]
        if any(h in lowered for h in ("category", "account", "line item", "item", "type")):
            records = [r for r in rows[i + 1 :] if any(c.strip() for c in r)]
            if len(records) > parsers.MAX_ROWS:
                raise HistoryError(f"Too many rows (max {parsers.MAX_ROWS}).")
            return [c.strip() for c in row], records
    if rows:
        return [c.strip() for c in rows[0]], [r for r in rows[1:] if any(c.strip() for c in r)]
    raise HistoryError("That file is empty.")


def guess_history_mapping(headers):
    hints = {
        "year": ["year", "fiscal year", "fy"],
        "date": ["date", "posting date", "transaction date"],
        "category": ["category", "account", "line item", "item", "type"],
        "amount": ["amount", "total", "actual", "actuals"],
        "description": ["description", "memo", "payee", "vendor", "notes"],
    }
    lowered = [h.lower() for h in headers]
    mapping = {}
    for key, options in hints.items():
        for option in options:
            if option in lowered and headers[lowered.index(option)] not in mapping.values():
                mapping[key] = headers[lowered.index(option)]
                break
    return mapping


@dataclass
class HistoryRow:
    line: int
    year: int | None = None
    category: str = ""
    amount_cents: int | None = None
    description: str = ""
    errors: list = field(default_factory=list)


def parse_history(headers, records, mapping, default_year=None):
    index = {h: i for i, h in enumerate(headers)}
    if "category" not in mapping or "amount" not in mapping:
        raise HistoryError("Choose the category and amount columns.")
    if "year" not in mapping and "date" not in mapping and not default_year:
        raise HistoryError("Choose a year or date column, or enter the year this file covers.")
    for col in mapping.values():
        if col not in index:
            raise HistoryError(f"Column '{col}' isn't in this file.")

    def cell(record, key):
        col = mapping.get(key)
        if not col:
            return ""
        i = index[col]
        return record[i].strip() if i < len(record) else ""

    rows = []
    for n, record in enumerate(records, start=2):
        row = HistoryRow(
            line=n, category=cell(record, "category")[:100], description=cell(record, "description")[:300]
        )
        if "year" in mapping:
            raw = cell(record, "year").upper().removeprefix("FY").strip()
            row.year = int(raw) if raw.isdigit() and 1900 < int(raw) < 2200 else None
            if row.year is None:
                row.errors.append(f"'{cell(record, 'year')}' isn't a year")
        elif "date" in mapping:
            try:
                row.year = parsers.parse_date(cell(record, "date")).year
            except ValueError as exc:
                row.errors.append(str(exc))
        else:
            row.year = int(default_year)
        try:
            row.amount_cents = parsers.parse_amount(cell(record, "amount"))
            if row.amount_cents is None:
                row.errors.append("missing amount")
        except ValueError as exc:
            row.errors.append(str(exc))
        if not row.category:
            row.errors.append("missing category")
        rows.append(row)
    return rows


def guess_category_map(categories, saved=None):
    """Old category name -> Account id, from a saved mapping or by matching names."""
    saved = saved or {}
    accounts = list(plannable_accounts()) + ([dues_account()] if dues_account() else [])
    by_name = {a.name.lower(): a.pk for a in accounts}
    by_code = {a.code: a.pk for a in accounts}
    result = {}
    for cat in categories:
        if cat in saved:
            result[cat] = saved[cat]
            continue
        key = cat.strip().lower()
        code = key.split()[0] if key.split() else ""
        result[cat] = (
            by_name.get(key)
            or by_code.get(code)
            or next((pk for name, pk in by_name.items() if key and (key in name or name in key)), None)
        )
    return result


def file_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


@transaction.atomic
def import_history(rows, category_map, amount_sign, filename, text, user, request=None):
    """Save parsed rows. category_map: old name -> Account (rows mapped to None are skipped)."""
    batch = HistoricalImport.objects.create(filename=filename[:200], sha256=file_hash(text), created_by=user)
    objs = []
    for row in rows:
        account = category_map.get(row.category)
        if not row.errors and account is not None:
            cents = row.amount_cents
            if amount_sign == "bank" and account.type == Account.Type.EXPENSE:
                cents = -cents  # bank style: money out is negative
            objs.append(
                HistoricalAmount(
                    batch=batch,
                    year=row.year,
                    account=account,
                    amount_cents=cents,
                    description=row.description,
                    source_category=row.category,
                )
            )
    HistoricalAmount.objects.bulk_create(objs, batch_size=500)
    batch.rows = len(objs)
    batch.save(update_fields=["rows"])
    audit.record(request or user, "budget.history_imported", batch, {"rows": batch.rows})
    return batch
