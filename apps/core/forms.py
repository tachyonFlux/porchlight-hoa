from django import forms
from django.contrib.auth import get_user_model, password_validation

from apps.accounts.models import BoardPosition

from . import setup_token
from .models import HOASettings


class HOASettingsForm(forms.ModelForm):
    class Meta:
        model = HOASettings
        fields = ["name", "short_name", "contact_email", "mailing_address", "require_board_two_step"]
        widgets = {"mailing_address": forms.Textarea(attrs={"rows": 3})}


class SetupForm(forms.Form):
    hoa_name = forms.CharField(label="Neighborhood / HOA name", max_length=150)
    full_name = forms.CharField(label="Your name", max_length=150)
    email = forms.EmailField(label="Your email")
    title = forms.ChoiceField(
        label="Your board role",
        choices=BoardPosition.Title.choices,
        initial=BoardPosition.Title.PRESIDENT,
    )
    password1 = forms.CharField(label="Password", widget=forms.PasswordInput, strip=False)
    password2 = forms.CharField(label="Password again", widget=forms.PasswordInput, strip=False)
    setup_token = forms.CharField(
        label="Setup token",
        required=False,
        widget=forms.PasswordInput,
        help_text="Printed in the server logs, and saved as setup-token.txt in the data folder "
        "(or the PORCHLIGHT_SETUP_TOKEN value, if you set one).",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if setup_token.required():
            setup_token.get()  # make sure it exists and has been logged
            self.fields["setup_token"].required = True
        else:
            del self.fields["setup_token"]

    def clean_setup_token(self):
        value = self.cleaned_data.get("setup_token", "")
        if not setup_token.matches(value):
            raise forms.ValidationError("That setup token doesn't match.")
        return value

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if get_user_model().objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def clean(self):
        data = super().clean()
        p1, p2 = data.get("password1"), data.get("password2")
        if p1 and p2 and p1 != p2:
            self.add_error("password2", "The passwords don't match.")
        elif p1:
            user = get_user_model()(email=data.get("email", ""), full_name=data.get("full_name", ""))
            try:
                password_validation.validate_password(p1, user)
            except forms.ValidationError as exc:
                self.add_error("password1", exc)
        return data


class SetupNameForm(forms.Form):
    """Used when an admin already exists (e.g. created with createsuperuser)."""

    hoa_name = forms.CharField(label="Neighborhood / HOA name", max_length=150)
