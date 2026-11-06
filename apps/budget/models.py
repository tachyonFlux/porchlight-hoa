from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q

from . import calc

PCT = {"max_digits": 6, "decimal_places": 2}


class HistoricalImport(models.Model):
    """One spreadsheet of past years' income and spending."""

    filename = models.CharField(max_length=200)
    sha256 = models.CharField(max_length=64, db_index=True)
    rows = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.filename


class HistoricalAmount(models.Model):
    """A past amount for a category. Positive = the category's normal direction
    (money in for income, money out for expenses); refunds are negative."""

    batch = models.ForeignKey(HistoricalImport, on_delete=models.CASCADE, related_name="amounts")
    year = models.PositiveSmallIntegerField(db_index=True)
    account = models.ForeignKey("finance.Account", on_delete=models.PROTECT, related_name="+")
    amount_cents = models.BigIntegerField()
    description = models.CharField(max_length=300, blank=True)
    source_category = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ["year", "account__code", "id"]


class HistoricalMapping(models.Model):
    """Remembered spreadsheet layout and old-category -> category mapping, for re-imports."""

    name = models.CharField(max_length=100, unique=True)
    columns = models.JSONField(default=dict)
    amount_sign = models.CharField(max_length=10, default="positive")
    category_map = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return self.name


class ReserveComponent(models.Model):
    """Something the reserves pay to replace: a fence, entrance sign, retaining wall..."""

    name = models.CharField(max_length=120)
    replacement_cost_cents = models.BigIntegerField("replacement cost")
    useful_life_years = models.PositiveSmallIntegerField(
        "useful life (years)", validators=[MinValueValidator(1), MaxValueValidator(100)]
    )
    remaining_life_years = models.PositiveSmallIntegerField(
        "remaining life (years)",
        validators=[MaxValueValidator(100)],
        help_text="Years left before it needs replacing, as of the year below.",
    )
    as_of_year = models.PositiveSmallIntegerField(
        help_text="The year the cost estimate and remaining life were measured."
    )
    annual_contribution_cents = models.BigIntegerField(
        "planned yearly contribution", default=0, help_text="Leave 0 to use the straight-line amount."
    )
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["remaining_life_years", "name"]

    def __str__(self):
        return self.name

    def as_calc(self):
        c = calc.Component(
            name=self.name,
            cost_cents=self.replacement_cost_cents,
            useful_life=self.useful_life_years,
            remaining_life=self.remaining_life_years,
            as_of_year=self.as_of_year,
        )
        c.annual_contribution_cents = self.annual_contribution_cents or c.straight_line_cents
        return c


class Scenario(models.Model):
    """A named budget for a fiscal year; several can be compared before one is adopted."""

    name = models.CharField(max_length=100)
    fiscal_year = models.PositiveSmallIntegerField(help_text="The year this budget is for.")
    base_year = models.PositiveSmallIntegerField(
        help_text="The most recent complete year of actuals to project from."
    )
    inflation_pct = models.DecimalField("inflation (% a year)", default=Decimal("3"), **PCT)
    contingency_pct = models.DecimalField(
        "contingency (% of operating expenses)",
        default=Decimal("5"),
        validators=[MinValueValidator(0)],
        **PCT,
    )
    collection_rate_pct = models.DecimalField(
        "collection rate (%)",
        default=Decimal("97"),
        validators=[MinValueValidator(Decimal("1")), MaxValueValidator(100)],
        help_text="Share of billed dues you expect to actually collect.",
        **PCT,
    )
    increase_cap_pct = models.DecimalField(
        "yearly increase cap (%)",
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
        help_text="Optional. If your covenants limit yearly dues increases, enter the limit to be warned.",
        **PCT,
    )
    future_increase_pct = models.DecimalField(
        "dues increase in later years (% a year)",
        default=Decimal("0"),
        help_text="Used only for the multi-year cash projection.",
        **PCT,
    )
    homes_count = models.PositiveIntegerField(
        "homes paying dues", null=True, blank=True, help_text="Leave blank to use the number of active homes."
    )
    round_to_dollar = models.BooleanField(default=True, help_text="Round dues up to a whole dollar.")
    starting_operating_cents = models.BigIntegerField("operating cash at the start of the year", default=0)
    min_operating_balance_cents = models.BigIntegerField("minimum operating balance", default=0)
    starting_reserve_cents = models.BigIntegerField("reserve balance at the start of the year", default=0)
    reserve_contribution_cents = models.BigIntegerField(
        "yearly reserve contribution",
        null=True,
        blank=True,
        help_text="Leave blank to use the total of the reserve components' contributions.",
    )
    projection_years = models.PositiveSmallIntegerField(
        default=5, validators=[MinValueValidator(1), MaxValueValidator(30)]
    )
    notes = models.TextField(blank=True)

    is_adopted = models.BooleanField(default=False)
    adopted_at = models.DateTimeField(null=True, blank=True)
    adopted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    adopted_per_period_cents = models.BigIntegerField(null=True, blank=True)
    # Dues per period before this budget was adopted, so "change vs current" stays meaningful.
    replaced_per_period_cents = models.BigIntegerField(null=True, blank=True)
    # What residents see: a frozen copy, so later edits or imports don't change a published budget.
    published_snapshot = models.JSONField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-fiscal_year", "name"]
        constraints = [
            models.UniqueConstraint(fields=["fiscal_year", "name"], name="unique_scenario_name"),
            models.UniqueConstraint(
                fields=["fiscal_year"], condition=Q(is_adopted=True), name="one_adopted_per_year"
            ),
            models.UniqueConstraint(
                fields=["fiscal_year"],
                condition=Q(published_snapshot__isnull=False),
                name="one_published_per_year",
            ),
        ]

    def __str__(self):
        return f"{self.fiscal_year}: {self.name}"

    @property
    def is_published(self):
        return self.published_snapshot is not None


class ScenarioLine(models.Model):
    class Method(models.TextChoices):
        INFLATION = calc.INFLATION, "Last year + inflation"
        SAME = calc.SAME, "Same as last year"
        AVERAGE = calc.AVERAGE, "Average of recent years"
        TREND = calc.TREND, "Recent trend"
        MANUAL = calc.MANUAL, "Enter an amount"
        EXCLUDE = calc.EXCLUDE, "Leave out"

    scenario = models.ForeignKey(Scenario, on_delete=models.CASCADE, related_name="lines")
    account = models.ForeignKey("finance.Account", on_delete=models.CASCADE, related_name="+")
    method = models.CharField(max_length=10, choices=Method.choices, default=Method.INFLATION)
    years = models.PositiveSmallIntegerField(
        default=3, validators=[MinValueValidator(2), MaxValueValidator(10)], help_text="For average/trend."
    )
    manual_cents = models.BigIntegerField(null=True, blank=True)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["account__code"]
        constraints = [models.UniqueConstraint(fields=["scenario", "account"], name="one_line_per_account")]


class OneTimeItem(models.Model):
    scenario = models.ForeignKey(Scenario, on_delete=models.CASCADE, related_name="one_time_items")
    year = models.PositiveSmallIntegerField()
    description = models.CharField(max_length=200)
    amount_cents = models.BigIntegerField(help_text="Positive amount.")
    is_income = models.BooleanField(default=False, help_text="Money coming in (e.g. a grant) instead of out.")
    from_reserve = models.BooleanField(
        default=False, help_text="Paid from (or added to) the reserve fund instead of operating cash."
    )

    class Meta:
        ordering = ["year", "id"]

    def __str__(self):
        return self.description
