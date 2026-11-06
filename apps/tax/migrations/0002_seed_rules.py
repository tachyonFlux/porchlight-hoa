from datetime import date
from decimal import Decimal

from django.db import migrations

SOURCE = (
    "IRS Instructions for Form 1120-H (2025): 60% gross income test, 90% expenditure test, "
    "$100 specific deduction, 30% rate (32% timeshare); Treas. Reg. 1.528-4: 85% of lots zoned residential."
)


def seed(apps, schema_editor):
    TaxYearConfig = apps.get_model("tax", "TaxYearConfig")
    # These rules have been unchanged for many years; 2025 was checked against the 2025
    # instructions on 2026-11-05. Earlier years carry the same values for convenience.
    for year in range(2020, 2026):
        TaxYearConfig.objects.get_or_create(
            year=year,
            defaults={
                "gross_income_test_pct": Decimal("60"),
                "expenditure_test_pct": Decimal("90"),
                "residential_test_pct": Decimal("85"),
                "specific_deduction_cents": 10000,
                "tax_rate_pct": Decimal("30"),
                "timeshare_tax_rate_pct": Decimal("32"),
                "source": SOURCE if year == 2025 else "Same as 2025 (not separately checked)",
                "verified_on": date(2026, 11, 5) if year == 2025 else None,
            },
        )


class Migration(migrations.Migration):
    dependencies = [("tax", "0001_initial")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
