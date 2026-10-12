from django import forms

from .models import Home, HomeDetail, Residency


class HomeForm(forms.ModelForm):
    class Meta:
        model = Home
        fields = ["address", "lot_number", "notes", "is_active"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}
        labels = {"is_active": "Active (part of the HOA)"}

    def clean_address(self):
        address = " ".join(self.cleaned_data["address"].split())
        qs = Home.objects.filter(address__iexact=address)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("There's already a home with this address.")
        return address


class AddResidentForm(forms.Form):
    email = forms.EmailField()
    full_name = forms.CharField(label="Name", max_length=150, required=False)
    phone = forms.CharField(max_length=40, required=False)
    relation = forms.ChoiceField(choices=Residency.Relation.choices)


class HomeDetailForm(forms.ModelForm):
    class Meta:
        model = HomeDetail
        fields = ["key", "value", "visibility"]
        widgets = {
            "key": forms.TextInput(attrs={"placeholder": "e.g. Dog's name, Gate code, Paint color"}),
            "value": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, home, allowed_levels, **kwargs):
        super().__init__(*args, **kwargs)
        self.home = home
        self.fields["visibility"].choices = [
            c for c in HomeDetail.Visibility.choices if c[0] in allowed_levels
        ]

    def clean_key(self):
        key = self.cleaned_data["key"].strip()
        qs = HomeDetail.objects.filter(home=self.home, key__iexact=key)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("This home already has a detail with that label.")
        return key


class InviteCodeForm(forms.Form):
    code = forms.CharField(
        label="Invite code",
        max_length=20,
        widget=forms.TextInput(
            attrs={"autocapitalize": "characters", "autocomplete": "off", "placeholder": "ABCD-EFGH"}
        ),
    )


class ClaimForm(forms.Form):
    full_name = forms.CharField(label="Your name", max_length=150)
    email = forms.EmailField(label="Your email")
    relation = forms.ChoiceField(
        label="You are the home's",
        choices=Residency.Relation.choices,
        initial=Residency.Relation.OWNER,
    )

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()


class ResidentImportForm(forms.Form):
    file = forms.FileField(
        label="CSV file",
        help_text="Columns: address, lot, owner name(s), email, phone. "
        "Separate multiple people in one cell with semicolons.",
    )

    def clean_file(self):
        f = self.cleaned_data["file"]
        if not f.name.lower().endswith(".csv"):
            raise forms.ValidationError("Please upload a .csv file.")
        return f
