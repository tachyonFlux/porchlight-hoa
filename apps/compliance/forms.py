from django import forms

from apps.accounts.models import board_members

from .models import Deadline


class DeadlineForm(forms.ModelForm):
    class Meta:
        model = Deadline
        fields = ["title", "category", "due_date", "recurrence", "owner", "remind_days", "description"]
        widgets = {
            "due_date": forms.DateInput(attrs={"type": "date"}),
            "description": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["owner"].queryset = board_members()
        self.fields["owner"].empty_label = "Whole board"


class CompleteForm(forms.Form):
    note = forms.CharField(
        required=False,
        max_length=300,
        label="Note (optional)",
        widget=forms.TextInput(attrs={"placeholder": "e.g. Filed online, confirmation #"}),
    )
