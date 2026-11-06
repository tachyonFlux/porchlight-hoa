from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

PCT = {"max_digits": 5, "decimal_places": 2, "validators": [MinValueValidator(0), MaxValueValidator(100)]}


class TaxYearConfig(models.Model):
    """Form 1120-H rules for one tax year. Check them against that year's IRS instructions:
    they have been stable for years, but Congress can change them."""

    year = models.PositiveSmallIntegerField(unique=True)
    gross_income_test_pct = models.DecimalField(
        "gross income test (%)",
        default=Decimal("60"),
        help_text="Minimum share of gross income that must be exempt function income.",
        **PCT,
    )
    expenditure_test_pct = models.DecimalField(
        "expenditure test (%)",
        default=Decimal("90"),
        help_text="Minimum share of spending that must go to the association's property.",
        **PCT,
    )
    residential_test_pct = models.DecimalField(
        "residential use test (%)",
        default=Decimal("85"),
        help_text="Minimum share of lots zoned residential (Treas. Reg. 1.528-4).",
        **PCT,
    )
    specific_deduction_cents = models.BigIntegerField("specific deduction", default=100_00)
    tax_rate_pct = models.DecimalField(
        "tax rate (%)", default=Decimal("30"), help_text="Homeowners and condominium associations.", **PCT
    )
    timeshare_tax_rate_pct = models.DecimalField("timeshare tax rate (%)", default=Decimal("32"), **PCT)
    source = models.CharField(
        max_length=300,
        blank=True,
        help_text="Where these numbers came from, e.g. 'IRS Instructions for Form 1120-H (2025)'.",
    )
    verified_on = models.DateField(null=True, blank=True, help_text="When someone last checked them.")
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["-year"]

    def __str__(self):
        return f"1120-H rules for {self.year}"

    @classmethod
    def for_year(cls, year):
        """(config, exact): this year's rules, or the most recent earlier year's as a fallback."""
        exact = cls.objects.filter(year=year).first()
        if exact:
            return exact, True
        return cls.objects.filter(year__lt=year).order_by("-year").first(), False


class Worksheet(models.Model):
    """The board's inputs for one year's 1120-H worksheet. Amounts come from the tax ledger."""

    class AssociationType(models.TextChoices):
        RESIDENTIAL = "residential", "Residential real estate management association (HOA)"
        CONDO = "condo", "Condominium management association"
        TIMESHARE = "timeshare", "Timeshare association"

    year = models.PositiveSmallIntegerField(unique=True)
    association_type = models.CharField(
        max_length=12, choices=AssociationType.choices, default=AssociationType.RESIDENTIAL
    )
    total_lots = models.PositiveIntegerField(
        "total lots (condos: total square feet)",
        null=True,
        blank=True,
        help_text="HOA: all lots, including unimproved ones (blank = number of homes). "
        "Condominium: total square footage of all units.",
    )
    residential_lots = models.PositiveIntegerField(
        "lots zoned residential (condos: square feet used as residences)",
        null=True,
        blank=True,
        help_text="Lots used for residents' pools, parking or similar count as residential. Blank = all.",
    )
    tax_exempt_interest_cents = models.BigIntegerField(
        "tax-exempt interest", default=0, help_text="Reported on item E, not as income."
    )
    no_private_benefit = models.BooleanField(
        "no member profits from the association's earnings",
        default=False,
        help_text="Other than through care of association property or a rebate of excess dues.",
    )
    # {account_id: line number} for non-exempt income and directly connected expenses
    line_map = models.JSONField(default=dict, blank=True)
    # account ids whose spending is NOT for acquiring/building/managing/maintaining/caring for property
    nonproperty_accounts = models.JSONField(default=list, blank=True)
    # {line number: {"cents": int, "note": str}} manual additions (e.g. capital gains, depreciation)
    adjustments = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-year"]

    def __str__(self):
        return f"1120-H worksheet {self.year}"
