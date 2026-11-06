from django import forms
from django.utils import timezone

from apps.core.money import MoneyField

from .models import TaxYearConfig, Worksheet


class WorksheetForm(forms.ModelForm):
    tax_exempt_interest_cents = MoneyField(
        label="Tax-exempt interest ($)", min_cents=0, help_text="Reported on item E, not as income."
    )

    class Meta:
        model = Worksheet
        fields = [
            "association_type",
            "total_lots",
            "residential_lots",
            "tax_exempt_interest_cents",
            "no_private_benefit",
            "notes",
        ]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}

    def clean(self):
        data = super().clean()
        total, res = data.get("total_lots"), data.get("residential_lots")
        if total is not None and res is not None and res > total:
            self.add_error("residential_lots", "Can't be more than the total number of lots.")
        return data


class TaxYearConfigForm(forms.ModelForm):
    specific_deduction_cents = MoneyField(label="Specific deduction ($)", min_cents=0)
    confirm = forms.BooleanField(
        label="I checked these against this year's IRS instructions for Form 1120-H",
    )

    class Meta:
        model = TaxYearConfig
        fields = [
            "year",
            "gross_income_test_pct",
            "expenditure_test_pct",
            "residential_test_pct",
            "specific_deduction_cents",
            "tax_rate_pct",
            "timeshare_tax_rate_pct",
            "source",
        ]

    def clean_year(self):
        year = self.cleaned_data["year"]
        if not 1990 <= year <= timezone.localdate().year + 1:
            raise forms.ValidationError("Enter a tax year between 1990 and next year.")
        return year
