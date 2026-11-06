from django import forms
from django.contrib.auth import get_user_model

from .models import BoardPosition

User = get_user_model()


class EmailForm(forms.Form):
    email = forms.EmailField(
        label="Your email",
        widget=forms.EmailInput(attrs={"autocomplete": "email", "autofocus": True}),
    )

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()


class PasswordLoginForm(forms.Form):
    email = forms.EmailField(widget=forms.EmailInput(attrs={"autocomplete": "email"}))
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}), strip=False
    )


class OTPForm(forms.Form):
    token = forms.CharField(
        label="6-digit code",
        max_length=8,
        widget=forms.TextInput(
            attrs={"inputmode": "numeric", "autocomplete": "one-time-code", "autofocus": True}
        ),
    )

    def clean_token(self):
        token = "".join(ch for ch in self.cleaned_data["token"] if ch in "0123456789")
        if len(token) != 6:
            raise forms.ValidationError("Enter the 6-digit code from your authenticator app.")
        return token


class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["full_name", "phone"]
        labels = {"full_name": "Name"}


class BoardPositionForm(forms.ModelForm):
    email = forms.EmailField(help_text="If there's no account with this email yet, one is created.")
    full_name = forms.CharField(required=False, label="Name (for new accounts)")

    class Meta:
        model = BoardPosition
        fields = ["title", "custom_title", "started_on"]
        widgets = {"started_on": forms.DateInput(attrs={"type": "date"})}
        labels = {"custom_title": "Custom title (if Other)"}

    field_order = ["email", "full_name", "title", "custom_title", "started_on"]
