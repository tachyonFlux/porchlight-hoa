from django import forms

from apps.core.uploads import MultipleImageField

from .models import MAX_PHOTOS, ExteriorRequest


class RequestForm(forms.ModelForm):
    photos = MultipleImageField(
        max_files=MAX_PHOTOS,
        help_text="Optional: photos of the area, a sketch, or a paint swatch (up to 10).",
    )

    class Meta:
        model = ExteriorRequest
        fields = ["title", "category", "description", "planned_start"]
        widgets = {
            "title": forms.TextInput(attrs={"placeholder": "e.g. Replace back fence"}),
            "description": forms.Textarea(attrs={"rows": 5}),
            "planned_start": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, homes=None, **kwargs):
        super().__init__(*args, **kwargs)
        if homes is not None and len(homes) > 1:
            self.fields["home"] = forms.ModelChoiceField(queryset=homes, label="Which home?")
            self.order_fields(["home"])


class CommentForm(forms.Form):
    note = forms.CharField(label="Add a comment", widget=forms.Textarea(attrs={"rows": 3}), max_length=4000)
    photos = MultipleImageField(max_files=MAX_PHOTOS, label="Add photos (optional)")
    board_only = forms.BooleanField(required=False, label="Board-only note (hidden from the household)")

    def __init__(self, *args, show_board_only=False, **kwargs):
        super().__init__(*args, **kwargs)
        if not show_board_only:
            del self.fields["board_only"]


class StatusForm(forms.Form):
    status = forms.ChoiceField(widget=forms.RadioSelect)
    note = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Shown to the household. For approvals, list any conditions.",
    )

    def __init__(self, *args, choices, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["status"].choices = choices
