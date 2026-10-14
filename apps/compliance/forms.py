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
        owners = board_members()
        if self.instance.owner_id:  # keep a former board member selectable so the form still saves
            owners = owners | type(self.instance.owner).objects.filter(pk=self.instance.owner_id)
        self.fields["owner"].queryset = owners.distinct()
        self.fields["owner"].empty_label = "Whole board"
        if self.instance.owner_id and not self.instance.owner.is_board:
            self.fields[
                "owner"
            ].help_text = (
                f"{self.instance.owner} is no longer on the board, so reminders go to the whole board."
            )


class CompleteForm(forms.Form):
    note = forms.CharField(
        required=False,
        max_length=300,
        label="Note (optional)",
        widget=forms.TextInput(attrs={"placeholder": "e.g. Filed online, confirmation #"}),
    )
