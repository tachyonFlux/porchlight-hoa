"""Pure-function tests: money, billing periods, ledger allocation."""

from datetime import date
from decimal import Decimal

import pytest

from apps.core.money import format_money, percent_of, to_cents
from apps.dues import ledger, periods
from apps.dues.ledger import Line


class TestMoney:
    @pytest.mark.parametrize(
        "value,cents",
        [("12.34", 1234), ("$1,234.5", 123450), (Decimal("0.005"), 1), ("0.004", 0), (300, 30000), ("", 0)],
    )
    def test_to_cents(self, value, cents):
        assert to_cents(value) == cents

    def test_bad_input(self):
        with pytest.raises(ValueError):
            to_cents("twelve")

    def test_format(self):
        assert format_money(123456) == "$1,234.56"
        assert format_money(-500) == "-$5.00"
        assert format_money(500, signed=True) == "+$5.00"

    def test_percent_rounds_half_up(self):
        assert percent_of(30000, Decimal("5")) == 1500
        assert percent_of(12345, Decimal("1.5")) == 185  # 185.175 -> 185
        assert percent_of(10, Decimal("5")) == 1  # 0.5 -> 1


class TestPeriods:
    def test_annual(self):
        p = periods.period_containing(date(2027, 6, 15), "annual", 1, 1)
        assert (p.start, p.end, p.due_date) == (date(2027, 1, 1), date(2027, 12, 31), date(2027, 1, 1))

    def test_fiscal_year_anchor(self):
        p = periods.period_containing(date(2027, 3, 1), "annual", 7, 15)
        assert (p.start, p.end, p.due_date) == (date(2026, 7, 1), date(2027, 6, 30), date(2026, 7, 15))

    def test_quarterly_and_due_day_clamped(self):
        p = periods.period_containing(date(2028, 2, 10), "quarterly", 2, 31)
        assert (p.start, p.end, p.due_date) == (date(2028, 2, 1), date(2028, 4, 30), date(2028, 2, 29))

    def test_semiannual_and_monthly(self):
        assert periods.period_containing(date(2027, 9, 9), "semiannual", 1, 1).start == date(2027, 7, 1)
        m = periods.period_containing(date(2027, 2, 9), "monthly", 1, 5)
        assert (m.start, m.end, m.due_date) == (date(2027, 2, 1), date(2027, 2, 28), date(2027, 2, 5))

    def _issue(self, today, **kw):
        defaults = dict(
            frequency="annual", anchor_month=1, due_day=1, lead_days=30, billing_start=date(2027, 1, 1)
        )
        defaults.update(kw)
        return periods.periods_to_issue(today, **defaults)

    def test_nothing_before_issue_date(self):
        assert self._issue(date(2026, 12, 1)) == []

    def test_issue_date_reached(self):
        assert [p.start for p in self._issue(date(2026, 12, 2))] == [date(2027, 1, 1)]

    def test_first_period_starts_on_or_after_billing_start(self):
        result = self._issue(date(2027, 12, 1), billing_start=date(2027, 3, 1))
        assert [p.start for p in result] == []  # 2027 period started before billing start
        result = self._issue(date(2027, 12, 2), billing_start=date(2027, 3, 1))
        assert [p.start for p in result] == [date(2028, 1, 1)]

    def test_catches_up_missed_periods(self):
        result = self._issue(date(2027, 7, 1), frequency="quarterly", billing_start=date(2027, 1, 1))
        assert [p.start for p in result] == [date(2027, 1, 1), date(2027, 4, 1), date(2027, 7, 1)]

    def test_no_billing_start_means_nothing(self):
        assert self._issue(date(2030, 1, 1), billing_start=None) == []

    def test_labels(self):
        spec = periods.period_containing(date(2027, 1, 1), "annual", 1, 1)
        assert periods.period_label(spec, "annual") == "2027 dues"
        spec = periods.period_containing(date(2027, 4, 1), "quarterly", 1, 1)
        assert periods.period_label(spec, "quarterly") == "Apr – Jun 2027 dues"


D = date


class TestLedger:
    def test_running_balance(self):
        lines = [Line(2, D(2027, 2, 1), -10000), Line(1, D(2027, 1, 1), 30000)]
        assert [b for _, b in ledger.running_balance(lines)] == [30000, 20000]

    def test_oldest_charge_paid_first(self):
        lines = [
            Line(1, D(2026, 1, 1), 30000, invoice_id=1, kind="invoice"),
            Line(2, D(2027, 1, 1), 30000, invoice_id=2, kind="invoice"),
            Line(3, D(2027, 1, 5), -40000, kind="payment"),
        ]
        assert ledger.allocate(lines) == {1: 0, 2: 20000}

    def test_as_of_ignores_later_lines(self):
        lines = [
            Line(1, D(2027, 1, 1), 30000, invoice_id=1, kind="invoice"),
            Line(2, D(2027, 3, 1), -30000, kind="payment"),
        ]
        assert ledger.invoice_unpaid(lines, 1, as_of=D(2027, 1, 31)) == 30000
        assert ledger.invoice_unpaid(lines, 1) == 0

    def test_void_cancels_payment(self):
        lines = [
            Line(1, D(2027, 1, 1), 30000, invoice_id=1, kind="invoice"),
            Line(2, D(2027, 1, 2), -30000, kind="payment"),
            Line(3, D(2027, 1, 9), 30000, kind="payment_void"),
        ]
        assert ledger.invoice_unpaid(lines, 1) == 30000
        assert sum(x.amount_cents for x in lines) == 30000

    def test_waiver_cancels_its_own_fee(self):
        lines = [
            Line(1, D(2026, 1, 1), 30000, invoice_id=1, kind="invoice"),  # older, unpaid
            Line(2, D(2027, 1, 1), 30000, invoice_id=2, kind="invoice"),
            Line(3, D(2027, 2, 1), 2500, invoice_id=2, kind="late_fee"),
            Line(4, D(2027, 2, 5), -2500, invoice_id=2, kind="late_fee_waiver"),
        ]
        unpaid = ledger.allocate(lines)
        assert unpaid == {1: 30000, 2: 30000, 3: 0}
        assert ledger.invoice_outstanding(lines, 2) == 30000

    def test_credit_carries_forward(self):
        lines = [
            Line(1, D(2026, 12, 1), -5000, kind="payment"),
            Line(2, D(2027, 1, 1), 30000, invoice_id=1, kind="invoice"),
        ]
        assert ledger.invoice_unpaid(lines, 1) == 25000
