from django import forms
from django.utils import timezone

from apps.core.money import MoneyField

from .models import Account, BankAccount, CategoryRule, StatementBalance

FIELD_LABELS = {
    "date": "Date",
    "description": "Description",
    "amount": "Amount (one column, negative = money out)",
    "withdrawal": "Withdrawals / debits",
    "deposit": "Deposits / credits",
    "check_number": "Check number",
    "memo": "Memo",
}


class BankAccountForm(forms.ModelForm):
    opening_balance_cents = MoneyField(label="Opening balance ($)")

    class Meta:
        model = BankAccount
        fields = ["name", "last4", "is_reserve", "opening_balance_cents", "opening_date", "is_active"]
        widgets = {"opening_date": forms.DateInput(attrs={"type": "date"})}
        help_texts = {"last4": "Only the last four digits. Never enter full account numbers or passwords."}

    def clean_last4(self):
        value = self.cleaned_data["last4"].strip()
        if value and (not value.isdigit() or len(value) != 4):
            raise forms.ValidationError("Enter exactly four digits, or leave blank.")
        return value


class AccountForm(forms.ModelForm):
    class Meta:
        model = Account
        fields = ["code", "name", "type", "tax_class", "is_active"]


class RuleForm(forms.ModelForm):
    class Meta:
        model = CategoryRule
        fields = ["match", "pattern", "direction", "account", "priority", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["account"].queryset = Account.objects.filter(is_active=True)


class UploadForm(forms.Form):
    bank_account = forms.ModelChoiceField(queryset=BankAccount.objects.filter(is_active=True))
    file = forms.FileField(help_text="CSV, OFX or QFX downloaded from your bank's website.")

    def clean_file(self):
        f = self.cleaned_data["file"]
        if not f.name.lower().endswith((".csv", ".ofx", ".qfx", ".txt")):
            raise forms.ValidationError("Upload a .csv, .ofx or .qfx file.")
        return f


class MappingForm(forms.Form):
    flip_sign = forms.BooleanField(required=False, label="Flip signs (use if deposits show up as negative)")
    date_format = forms.ChoiceField(
        required=False,
        choices=[
            ("", "Detect automatically"),
            ("%m/%d/%Y", "MM/DD/YYYY"),
            ("%m/%d/%y", "MM/DD/YY"),
            ("%Y-%m-%d", "YYYY-MM-DD"),
            ("%d/%m/%Y", "DD/MM/YYYY"),
        ],
    )
    save_as = forms.CharField(required=False, max_length=100, label="Remember this layout as (optional)")

    def __init__(self, *args, headers, initial_mapping=None, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [("", "(not in this file)")] + [(h, h) for h in headers]
        for key, label in FIELD_LABELS.items():
            self.fields[f"col_{key}"] = forms.ChoiceField(
                choices=choices, required=False, label=label, initial=(initial_mapping or {}).get(key, "")
            )
        self.order_fields([f"col_{k}" for k in FIELD_LABELS] + ["flip_sign", "date_format", "save_as"])

    def mapping(self):
        return {k: self.cleaned_data[f"col_{k}"] for k in FIELD_LABELS if self.cleaned_data.get(f"col_{k}")}


class StatementForm(forms.ModelForm):
    ending_balance_cents = MoneyField(label="Ending balance on the statement ($)")

    class Meta:
        model = StatementBalance
        fields = ["statement_date", "ending_balance_cents", "note"]
        widgets = {"statement_date": forms.DateInput(attrs={"type": "date"})}

    def clean_statement_date(self):
        d = self.cleaned_data["statement_date"]
        if d > timezone.localdate():
            raise forms.ValidationError("That date is in the future.")
        return d


class SimpleFINConnectForm(forms.Form):
    setup_token = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3, "autocomplete": "off", "spellcheck": "false"}),
        help_text="Paste the one-time setup token from the SimpleFIN Bridge website. It's used once and "
        "then discarded. Never paste your bank password anywhere in Porchlight.",
    )
