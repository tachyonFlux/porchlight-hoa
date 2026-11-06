"""Form 1120-H draft worksheet, built from the tax ledger.

This is a worksheet to help the treasurer and the association's tax preparer. It is not
tax advice and doesn't file anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

from apps.core.money import format_money
from apps.finance.models import Account
from apps.finance.services import ledger_summary

from .models import TaxYearConfig, Worksheet

INCOME_LINES = {
    1: "Dividends",
    2: "Taxable interest",
    3: "Gross rents",
    4: "Gross royalties",
    5: "Capital gain net income (attach Schedule D)",
    6: "Net gain or (loss) from Form 4797",
    7: "Other income (excluding exempt function income)",
}
DEDUCTION_LINES = {
    9: "Salaries and wages",
    10: "Repairs and maintenance",
    11: "Rents",
    12: "Taxes and licenses",
    13: "Interest",
    14: "Depreciation",
    15: "Other deductions (attach statement)",
}
LINE_LABELS = INCOME_LINES | DEDUCTION_LINES

DISCLAIMER = "Draft worksheet, not tax advice; review before filing."


def guess_income_line(account):
    name = account.name.lower()
    for word, line in (("dividend", 1), ("interest", 2), ("rent", 3), ("royalt", 4)):
        if word in name:
            return line
    return 7


def guess_deduction_line(account):
    name = account.name.lower()
    for words, line in (
        (("salar", "wage", "payroll"), 9),
        (("repair", "maint"), 10),
        (("rent",), 11),
        (("tax", "licens"), 12),
        (("interest",), 13),
        (("depreciation",), 14),
    ):
        if any(w in name for w in words):
            return line
    return 15


def default_nonproperty(accounts):
    """Spending that usually isn't for the association's property (social events)."""
    return [a.pk for a in accounts if any(w in a.name.lower() for w in ("event", "social", "party"))]


def get_worksheet(year):
    from apps.community.models import Home

    ws = Worksheet.objects.filter(year=year).first()
    if ws is None:
        ws = Worksheet(year=year)
        expenses = Account.objects.filter(type=Account.Type.EXPENSE)
        ws.nonproperty_accounts = default_nonproperty(expenses)
        ws.total_lots = ws.residential_lots = Home.objects.filter(is_active=True).count() or None
    return ws


def due_date(year):
    """15th day of the 4th month after a calendar tax year, moved past a weekend.
    (Federal holidays can move it too; check the IRS calendar.)"""
    due = date(year + 1, 4, 15)
    while due.weekday() >= 5:
        due += timedelta(days=1)
    return due


def _pct(part, whole):
    if not whole:
        return None
    return (Decimal(part) * 100 / Decimal(whole)).quantize(Decimal("0.1"))


def _fmt(pct):
    text = f"{pct:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _rate(cents, pct):
    return int((Decimal(cents) * Decimal(pct) / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass
class Test:
    label: str
    actual_pct: Decimal | None
    required_pct: Decimal
    detail: str

    @property
    def passed(self):
        return self.actual_pct is not None and self.actual_pct >= self.required_pct


@dataclass
class LineItem:
    account: Account | None
    line: int
    cents: int
    note: str = ""


@dataclass
class Result:
    year: int
    worksheet: Worksheet
    config: TaxYearConfig | None
    config_exact: bool
    exempt_income: list
    nonexempt_income: list  # LineItems
    deductions: list  # LineItems
    lines: dict  # line number -> cents (1..22 as applicable)
    tests: list
    total_expenses_cents: int
    property_expenses_cents: int
    uncategorized: object
    unclassified_income: list = field(default_factory=list)  # income accounts tagged "other"
    warnings: list = field(default_factory=list)

    @property
    def qualifies(self):
        return all(t.passed for t in self.tests) and self.worksheet.no_private_benefit

    @property
    def due(self):
        return due_date(self.year)

    @property
    def rate_pct(self):
        if not self.config:
            return None
        if self.worksheet.association_type == Worksheet.AssociationType.TIMESHARE:
            return self.config.timeshare_tax_rate_pct
        return self.config.tax_rate_pct


def compute(ws):
    year = ws.year
    config, exact = TaxYearConfig.for_year(year)
    summary = ledger_summary(year)
    T = Account.TaxClass
    line_map = {int(k): int(v) for k, v in (ws.line_map or {}).items()}
    nonproperty = set(ws.nonproperty_accounts or [])

    exempt, nonexempt, unclassified = [], [], []
    for item in summary["income"]:
        acct = item.account
        if acct.tax_class == T.EXEMPT_INCOME:
            exempt.append(item)
            continue
        if acct.tax_class != T.NONEXEMPT_INCOME:
            # Not tagged either way: treat as taxable (the safer reading) and flag it.
            unclassified.append(acct)
        line = line_map.get(acct.pk) or guess_income_line(acct)
        nonexempt.append(LineItem(acct, line if line in INCOME_LINES else 7, item.net_cents))

    deductions = []
    total_exp = prop_exp = 0
    for item in summary["expense"]:
        acct = item.account
        total_exp += item.net_cents
        if acct.pk not in nonproperty:
            prop_exp += item.net_cents
        if acct.tax_class == T.NONEXEMPT_EXPENSE:
            line = line_map.get(acct.pk) or guess_deduction_line(acct)
            deductions.append(LineItem(acct, line if line in DEDUCTION_LINES else 15, item.net_cents))

    lines = dict.fromkeys(list(INCOME_LINES) + list(DEDUCTION_LINES), 0)
    for li in nonexempt + deductions:
        lines[li.line] += li.cents
    adjustments = []
    for key, adj in (ws.adjustments or {}).items():
        line = int(key)
        cents = int(adj.get("cents") or 0)
        if line in lines and cents:
            lines[line] += cents
            adjustments.append(LineItem(None, line, cents, adj.get("note", "")))
    lines[8] = sum(lines[n] for n in INCOME_LINES)
    lines[16] = sum(lines[n] for n in DEDUCTION_LINES)
    lines[17] = lines[8] - lines[16]
    specific = config.specific_deduction_cents if config else 0
    lines[18] = min(specific, max(lines[17], 0))
    lines[19] = max(lines[17] - lines[18], 0)

    exempt_total = sum(i.net_cents for i in exempt)
    gross_total = exempt_total + lines[8]
    tests = []
    if config:
        tests.append(
            Test(
                f"{_fmt(config.gross_income_test_pct)}% gross income test",
                _pct(exempt_total, gross_total),
                config.gross_income_test_pct,
                f"Exempt function income {format_money(exempt_total)} of gross income "
                f"{format_money(gross_total)}",
            )
        )
        tests.append(
            Test(
                f"{_fmt(config.expenditure_test_pct)}% expenditure test",
                _pct(prop_exp, total_exp),
                config.expenditure_test_pct,
                f"Spending on association property {format_money(prop_exp)} of total spending "
                f"{format_money(total_exp)}",
            )
        )
        total_lots = ws.total_lots or 0
        residential = ws.residential_lots if ws.residential_lots is not None else total_lots
        tests.append(
            Test(
                f"{_fmt(config.residential_test_pct)}% residential use test",
                _pct(residential, total_lots),
                config.residential_test_pct,
                f"{residential} of {total_lots} lots zoned residential",
            )
        )

    result = Result(
        year=year,
        worksheet=ws,
        config=config,
        config_exact=exact,
        exempt_income=exempt,
        nonexempt_income=nonexempt + adjustments_for(adjustments, INCOME_LINES),
        deductions=deductions + adjustments_for(adjustments, DEDUCTION_LINES),
        lines=lines,
        tests=tests,
        total_expenses_cents=total_exp,
        property_expenses_cents=prop_exp,
        uncategorized=summary["uncategorized"],
        unclassified_income=unclassified,
    )
    rate = result.rate_pct
    lines[20] = _rate(lines[19], rate) if rate is not None else 0

    w = result.warnings
    if year >= timezone.localdate().year:
        w.append(f"{year} isn't over yet, so these numbers are incomplete.")
    if config is None:
        w.append(f"There are no 1120-H rules entered for {year} or earlier. Add them under Tax settings.")
    elif not exact:
        w.append(
            f"No 1120-H rules are entered for {year}; using {config.year}'s. Check the {year} IRS "
            "instructions and add that year under Tax settings."
        )
    if summary["uncategorized"]:
        w.append(
            f"{summary['uncategorized'].count} transactions in {year} aren't categorized, "
            "so they're left out."
        )
    if unclassified:
        w.append(
            "These income categories aren't tagged as exempt or non-exempt, so they're counted as taxable: "
            + ", ".join(a.name for a in unclassified)
            + ". Set their tax class under Finances → Categories."
        )
    if not ws.no_private_benefit:
        w.append("Confirm that no member profits from the association's net earnings (see below).")
    if not exempt and not nonexempt:
        w.append(f"There's no income in the tax ledger for {year}.")
    return result


def adjustments_for(adjustments, lines):
    return [a for a in adjustments if a.line in lines]
