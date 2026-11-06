"""Budget math: projections, reserve funding and the dues calculator.

Pure functions on integer cents (no database), so they're easy to test and reason about.
Percentages are Decimals or anything Decimal() accepts ("3.5" means 3.5%).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

SAME = "same"
INFLATION = "inflation"
AVERAGE = "average"
TREND = "trend"
MANUAL = "manual"
EXCLUDE = "exclude"


def _d(value):
    return Decimal(str(value or 0))


def _round(value, mode=ROUND_HALF_UP):
    return int(Decimal(value).quantize(Decimal("1"), rounding=mode))


def grow(cents, pct, years):
    """`cents` grown by `pct`% a year, compounded, for `years` years (negative = deflate)."""
    if not years:
        return int(cents)
    return _round(_d(cents) * (1 + _d(pct) / 100) ** years)


def _window(history, base_year, n_years):
    """The last `n_years` years of history up to and including the base year."""
    years = sorted(y for y in history if y <= base_year)[-max(int(n_years or 1), 1) :]
    return [(y, history[y]) for y in years]


def linear_fit(points):
    """Least-squares line through [(x, y)]. Returns (slope, intercept) as Decimals."""
    n = len(points)
    mean_x = sum(_d(x) for x, _ in points) / n
    mean_y = sum(_d(y) for _, y in points) / n
    sxx = sum((_d(x) - mean_x) ** 2 for x, _ in points)
    if sxx == 0:
        return Decimal(0), mean_y
    slope = sum((_d(x) - mean_x) * (_d(y) - mean_y) for x, y in points) / sxx
    return slope, mean_y - slope * mean_x


def project(method, history, *, base_year, fiscal_year, year, inflation_pct=0, n_years=3, manual_cents=None):
    """Projected amount for one category in `year`.

    history: {year: cents} of actuals (years with no activity should be present as 0).
    - same: the base year's actual, flat.
    - inflation: the base year's actual grown by inflation for each year after it.
    - average: the average of the last N years up to the base year, flat.
    - trend: a straight line fitted through the last N years, extended (never below 0).
    - manual: the amount entered for the fiscal year, grown by inflation for later years.
    - exclude: always 0 (e.g. spending paid from reserves).
    """
    if method == EXCLUDE:
        return 0
    if method == MANUAL:
        return grow(manual_cents or 0, inflation_pct, year - fiscal_year)
    if method == SAME:
        return int(history.get(base_year, 0))
    if method == INFLATION:
        return grow(history.get(base_year, 0), inflation_pct, year - base_year)
    window = _window(history, base_year, n_years)
    if not window:
        return 0
    if method == AVERAGE:
        return _round(sum(_d(v) for _, v in window) / len(window))
    if method == TREND:
        if len(window) < 2:
            return int(window[0][1])
        slope, intercept = linear_fit(window)
        return max(0, _round(intercept + slope * year))
    raise ValueError(f"Unknown projection method: {method}")


# ------------------------------------------------------------------ reserves


@dataclass
class Component:
    name: str
    cost_cents: int  # replacement cost in `as_of_year` dollars
    useful_life: int
    remaining_life: int  # years left as of `as_of_year`
    as_of_year: int
    annual_contribution_cents: int = 0

    def remaining_in(self, year):
        """Years left in `year`. After the first replacement it's assumed to be replaced on
        schedule every useful life (the same assumption the reserve projection makes)."""
        first = self.replacement_year
        if year <= first:
            return first - year
        cycle = (year - first) % max(int(self.useful_life), 1)
        return 0 if cycle == 0 else self.useful_life - cycle

    @property
    def replacement_year(self):
        return self.as_of_year + max(self.remaining_life, 0)

    def cost_in(self, year, inflation_pct):
        return grow(self.cost_cents, inflation_pct, year - self.as_of_year)

    def fully_funded_cents(self, year=None, inflation_pct=0):
        """What should be saved by now: the used-up share of the replacement cost."""
        year = self.as_of_year if year is None else year
        life = max(int(self.useful_life), 1)
        remaining = min(max(self.remaining_in(year), 0), life)
        return _round(_d(self.cost_in(year, inflation_pct)) * (life - remaining) / life)

    @property
    def straight_line_cents(self):
        """Yearly amount that saves the full cost over one useful life."""
        return _round(_d(self.cost_cents) / max(int(self.useful_life), 1), ROUND_CEILING)


def fully_funded_total(components, year=None, inflation_pct=0):
    return sum(c.fully_funded_cents(year, inflation_pct) for c in components)


def percent_funded(balance_cents, components, year=None, inflation_pct=0):
    """Reserve balance as a percent of the fully funded balance (None if nothing to fund)."""
    target = fully_funded_total(components, year, inflation_pct)
    if target <= 0:
        return None
    return (_d(balance_cents) * 100 / target).quantize(Decimal("0.1"))


@dataclass
class ReserveYear:
    year: int
    start_cents: int
    contribution_cents: int
    spending_cents: int
    end_cents: int
    replacements: list = field(default_factory=list)  # [(name, cents)]

    @property
    def short(self):
        return self.end_cents < 0


def reserve_projection(
    start_cents, components, start_year, years, inflation_pct=0, contributions=None, extra=None
):
    """Year-by-year reserve balance.

    contributions: {year: cents} paid in each year (default: the components' annual amounts).
    extra: {year: cents} other reserve spending (positive) or income (negative), e.g. one-time items.
    A component is replaced in its replacement year at its inflated cost, then again every
    useful life after that.
    """
    rows = []
    balance = int(start_cents)
    default_contribution = sum(c.annual_contribution_cents for c in components)
    for year in range(start_year, start_year + years):
        contribution = (contributions or {}).get(year, default_contribution)
        replacements = []
        for c in components:
            first = c.replacement_year
            life = max(int(c.useful_life), 1)
            if year >= first and (year - first) % life == 0:
                replacements.append((c.name, c.cost_in(year, inflation_pct)))
        spending = sum(cents for _, cents in replacements) + (extra or {}).get(year, 0)
        end = balance + contribution - spending
        rows.append(ReserveYear(year, balance, contribution, spending, end, replacements))
        balance = end
    return rows


# ------------------------------------------------------------------ dues calculator


@dataclass
class DuesResult:
    opex_cents: int
    reserve_cents: int
    contingency_cents: int
    other_income_cents: int
    net_needed_cents: int  # what dues must actually bring in
    gross_needed_cents: int  # billed, after allowing for unpaid dues
    homes: int
    periods_per_year: int
    per_period_cents: int
    annual_cents: int  # per home
    current_annual_cents: int
    change_cents: int
    change_pct: Decimal | None
    cap_pct: Decimal | None = None
    over_cap: bool = False
    capped_annual_cents: int | None = None
    capped_per_period_cents: int | None = None
    capped_shortfall_cents: int = 0  # collected shortfall if dues are held to the cap


def dues_needed(
    *,
    opex_cents,
    reserve_cents,
    contingency_pct,
    other_income_cents,
    collection_rate_pct,
    homes,
    periods_per_year=1,
    current_annual_cents=0,
    cap_pct=None,
    round_to_dollar=True,
):
    """(opex + reserve contribution + contingency) - other income, grossed up for the share
    of dues that won't be collected, divided across homes and billing periods.

    Contingency is a percent of operating expenses. Amounts are rounded up so the budget
    is never short by a rounding cent (or dollar, with round_to_dollar).
    """
    if homes <= 0:
        raise ValueError("There must be at least one home.")
    rate = _d(collection_rate_pct)
    if not 0 < rate <= 100:
        raise ValueError("Collection rate must be more than 0% and at most 100%.")
    periods = max(int(periods_per_year), 1)
    contingency = _round(_d(opex_cents) * _d(contingency_pct) / 100)
    net = max(0, opex_cents + reserve_cents + contingency - other_income_cents)
    gross = _round(_d(net) * 100 / rate, ROUND_CEILING)
    per_period = _round(_d(gross) / (homes * periods), ROUND_CEILING)
    if round_to_dollar:
        per_period = _round(_d(per_period) / 100, ROUND_CEILING) * 100
    annual = per_period * periods
    change = annual - current_annual_cents
    change_pct = (
        (_d(change) * 100 / current_annual_cents).quantize(Decimal("0.1")) if current_annual_cents else None
    )
    result = DuesResult(
        opex_cents=opex_cents,
        reserve_cents=reserve_cents,
        contingency_cents=contingency,
        other_income_cents=other_income_cents,
        net_needed_cents=net,
        gross_needed_cents=gross,
        homes=homes,
        periods_per_year=periods,
        per_period_cents=per_period,
        annual_cents=annual,
        current_annual_cents=current_annual_cents,
        change_cents=change,
        change_pct=change_pct,
        cap_pct=_d(cap_pct) if cap_pct not in (None, "") else None,
    )
    if result.cap_pct is not None and current_annual_cents > 0:
        cap_per_period = _round(
            _d(current_annual_cents) * (1 + result.cap_pct / 100) / periods / (100 if round_to_dollar else 1),
            ROUND_FLOOR,
        ) * (100 if round_to_dollar else 1)
        # Rounding down to a dollar must never put the "capped" amount below today's dues.
        cap_per_period = max(cap_per_period, _round(_d(current_annual_cents) / periods, ROUND_CEILING))
        capped_annual = cap_per_period * periods
        if annual > capped_annual:
            result.over_cap = True
            result.capped_per_period_cents = cap_per_period
            result.capped_annual_cents = capped_annual
            result.capped_shortfall_cents = _round(
                _d(annual - capped_annual) * homes * rate / 100, ROUND_HALF_UP
            )
    return result


def collected(annual_per_home_cents, homes, collection_rate_pct):
    """Dues actually expected to come in."""
    return _round(_d(annual_per_home_cents) * homes * _d(collection_rate_pct) / 100)


@dataclass
class CashYear:
    year: int
    start_cents: int
    dues_cents: int
    other_income_cents: int
    expense_cents: int
    reserve_transfer_cents: int
    end_cents: int
    dues_per_home_cents: int
    below_minimum: bool = False


def operating_projection(
    *,
    start_cents,
    years,
    dues_per_home,
    homes,
    collection_rate_pct,
    other_income,
    expenses,
    reserve_transfers,
    min_balance_cents=0,
):
    """Operating cash year by year. All inputs except start/homes/rate are {year: cents}."""
    rows = []
    balance = int(start_cents)
    for year in years:
        dues = collected(dues_per_home[year], homes, collection_rate_pct)
        end = (
            balance
            + dues
            + other_income.get(year, 0)
            - expenses.get(year, 0)
            - reserve_transfers.get(year, 0)
        )
        rows.append(
            CashYear(
                year=year,
                start_cents=balance,
                dues_cents=dues,
                other_income_cents=other_income.get(year, 0),
                expense_cents=expenses.get(year, 0),
                reserve_transfer_cents=reserve_transfers.get(year, 0),
                end_cents=end,
                dues_per_home_cents=dues_per_home[year],
                below_minimum=end < min_balance_cents,
            )
        )
        balance = end
    return rows
