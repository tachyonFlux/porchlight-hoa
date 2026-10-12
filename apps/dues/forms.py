import calendar

from django import forms
from django.utils import timezone

from apps.community.models import Home
from apps.core.money import MoneyField

from .models import DuesSettings, PaymentMethod

MONTHS = [(i, calendar.month_name[i]) for i in range(1, 13)]


class DuesSettingsForm(forms.ModelForm):
    amount_cents = MoneyField(label="Dues per period ($)", min_cents=0)
    late_fee_flat_cents = MoneyField(label="Flat late fee ($)", min_cents=0, required=False)

    class Meta:
        model = DuesSettings
        fields = [
            "amount_cents",
            "frequency",
            "anchor_month",
            "due_day",
            "grace_days",
            "late_fee_type",
            "late_fee_flat_cents",
            "late_fee_percent",
            "payable_to",
            "payment_instructions",
            "auto_invoicing",
            "billing_start_date",
            "invoice_lead_days",
            "email_new_invoices",
            "reminder_days_before",
            "overdue_reminder_every_days",
            "overdue_reminder_max",
        ]
        widgets = {
            "billing_start_date": forms.DateInput(attrs={"type": "date"}),
            "payment_instructions": forms.Textarea(attrs={"rows": 3}),
            "anchor_month": forms.Select(choices=MONTHS),
        }
        labels = {
            "late_fee_percent": "Late fee percent (%)",
            "auto_invoicing": "Create invoices automatically",
        }

    def clean_late_fee_flat_cents(self):
        return self.cleaned_data.get("late_fee_flat_cents") or 0

    def clean(self):
        data = super().clean()
        if data.get("auto_invoicing"):
            if not data.get("billing_start_date"):
                self.add_error("billing_start_date", "Choose when automatic invoices should start.")
            if not data.get("amount_cents"):
                self.add_error("amount_cents", "Set the dues amount before turning on automatic invoices.")
        return data


class PaymentForm(forms.Form):
    home = forms.ModelChoiceField(queryset=Home.objects.filter(is_active=True))
    amount_cents = MoneyField(label="Amount ($)", min_cents=1)
    method = forms.ChoiceField(choices=PaymentMethod.choices, initial=PaymentMethod.CHECK)
    check_number = forms.CharField(label="Check #", max_length=30, required=False)
    received_on = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), initial=timezone.localdate)
    note = forms.CharField(max_length=200, required=False)

    def __init__(self, *args, home=None, **kwargs):
        super().__init__(*args, **kwargs)
        if home is not None:
            self.fields["home"].initial = home
            self.fields["home"].widget = forms.HiddenInput()

    def clean_received_on(self):
        d = self.cleaned_data["received_on"]
        if d > timezone.localdate():
            raise forms.ValidationError("That date is in the future.")
        return d

    def clean(self):
        data = super().clean()
        if data.get("method") == PaymentMethod.CHECK and not data.get("check_number"):
            self.add_error("check_number", "Enter the check number (it helps match bank deposits).")
        return data


class AdjustmentForm(forms.Form):
    KIND_CHOICES = [
        ("credit", "Credit (reduces what they owe)"),
        ("charge", "Charge (adds to what they owe)"),
    ]
    kind = forms.ChoiceField(choices=KIND_CHOICES, widget=forms.RadioSelect, initial="credit")
    amount_cents = MoneyField(label="Amount ($)", min_cents=1)
    memo = forms.CharField(max_length=200, help_text="e.g. 'Opening balance from old spreadsheet'")
    entry_date = forms.DateField(
        label="Date", widget=forms.DateInput(attrs={"type": "date"}), initial=timezone.localdate
    )

    def signed_cents(self):
        cents = self.cleaned_data["amount_cents"]
        return cents if self.cleaned_data["kind"] == "charge" else -cents


class VoidForm(forms.Form):
    reason = forms.CharField(max_length=200, help_text="e.g. 'Check bounced' or 'Entered twice'")


class WaiveForm(forms.Form):
    reason = forms.CharField(max_length=200, required=False)


class PeriodBillForm(forms.Form):
    homes = forms.ModelMultipleChoiceField(
        queryset=Home.objects.none(), widget=forms.CheckboxSelectMultiple, required=True
    )

    def __init__(self, *args, period, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["homes"].queryset = Home.objects.filter(is_active=True).exclude(invoices__period=period)


class NewPeriodForm(forms.Form):
    """Create a billing period by hand for a date (uses the dues settings for the rest)."""

    date = forms.DateField(
        label="A date inside the period",
        widget=forms.DateInput(attrs={"type": "date"}),
        initial=timezone.localdate,
    )
