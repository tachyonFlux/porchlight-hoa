from django import forms
from django.utils import timezone

from apps.core.money import MoneyField

from .models import OneTimeItem, ReserveComponent, Scenario
from .services import HISTORY_FIELDS


class ScenarioForm(forms.ModelForm):
    starting_operating_cents = MoneyField(label="Operating cash at the start of the year ($)")
    min_operating_balance_cents = MoneyField(
        label="Minimum operating balance ($)",
        min_cents=0,
        help_text="Years projected to end below this are flagged.",
    )
    starting_reserve_cents = MoneyField(label="Reserve balance at the start of the year ($)")
    reserve_contribution_cents = MoneyField(
        label="Yearly reserve contribution ($)",
        required=False,
        min_cents=0,
        help_text="Leave blank to use the total of the reserve components' planned contributions.",
    )

    class Meta:
        model = Scenario
        fields = [
            "name",
            "fiscal_year",
            "base_year",
            "inflation_pct",
            "contingency_pct",
            "collection_rate_pct",
            "increase_cap_pct",
            "homes_count",
            "round_to_dollar",
            "starting_operating_cents",
            "min_operating_balance_cents",
            "starting_reserve_cents",
            "reserve_contribution_cents",
            "projection_years",
            "future_increase_pct",
            "notes",
        ]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}

    def clean(self):
        data = super().clean()
        fy, base = data.get("fiscal_year"), data.get("base_year")
        this_year = timezone.localdate().year
        if fy and not this_year - 5 <= fy <= this_year + 5:
            self.add_error("fiscal_year", "Choose a year within five years of now.")
        if fy and base and base >= fy:
            self.add_error("base_year", "The base year has to be before the budget year.")
        return data


class OneTimeItemForm(forms.ModelForm):
    amount_cents = MoneyField(label="Amount ($)", min_cents=1)

    class Meta:
        model = OneTimeItem
        fields = ["year", "description", "amount_cents", "is_income", "from_reserve"]

    def __init__(self, *args, scenario, **kwargs):
        super().__init__(*args, **kwargs)
        self.scenario = scenario
        years = range(scenario.fiscal_year, scenario.fiscal_year + scenario.projection_years)
        self.fields["year"] = forms.TypedChoiceField(choices=[(y, y) for y in years], coerce=int)


class ReserveComponentForm(forms.ModelForm):
    replacement_cost_cents = MoneyField(label="Replacement cost ($)", min_cents=0)
    annual_contribution_cents = MoneyField(
        label="Planned yearly contribution ($)",
        required=False,
        min_cents=0,
        help_text="Leave blank or 0 to use the straight-line amount (cost ÷ useful life).",
    )

    class Meta:
        model = ReserveComponent
        fields = [
            "name",
            "replacement_cost_cents",
            "useful_life_years",
            "remaining_life_years",
            "as_of_year",
            "annual_contribution_cents",
            "notes",
            "is_active",
        ]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}

    def clean_annual_contribution_cents(self):
        return self.cleaned_data.get("annual_contribution_cents") or 0

    def clean(self):
        data = super().clean()
        useful, remaining = data.get("useful_life_years"), data.get("remaining_life_years")
        if useful and remaining is not None and remaining > useful:
            self.add_error("remaining_life_years", "Remaining life can't be longer than the useful life.")
        return data


class HistoryUploadForm(forms.Form):
    file = forms.FileField(
        help_text="A CSV with one row per amount: a category, an amount, and a year (or date) column."
    )

    def clean_file(self):
        f = self.cleaned_data["file"]
        if not f.name.lower().endswith((".csv", ".txt")):
            raise forms.ValidationError("Upload a .csv file (in a spreadsheet app, use 'Save as CSV').")
        return f


class HistoryMapForm(forms.Form):
    default_year = forms.IntegerField(
        required=False,
        min_value=1900,
        max_value=2200,
        label="Year this file covers",
        help_text="Only needed if the file has no year or date column.",
    )
    amount_sign = forms.ChoiceField(
        choices=[
            ("positive", "Amounts are positive (spending shown as a positive number)"),
            ("bank", "Spending is negative, like a bank statement"),
        ],
        initial="positive",
    )
    save_as = forms.CharField(required=False, max_length=100, label="Remember this layout as (optional)")

    def __init__(self, *args, headers, initial_mapping=None, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [("", "(not in this file)")] + [(h, h) for h in headers]
        for key, label in HISTORY_FIELDS.items():
            self.fields[f"col_{key}"] = forms.ChoiceField(
                choices=choices, required=False, label=label, initial=(initial_mapping or {}).get(key, "")
            )
        self.order_fields([f"col_{k}" for k in HISTORY_FIELDS] + ["default_year", "amount_sign", "save_as"])

    def mapping(self):
        return {k: self.cleaned_data[f"col_{k}"] for k in HISTORY_FIELDS if self.cleaned_data.get(f"col_{k}")}
