from datetime import timedelta

from django import forms
from django.utils import timezone

from apps.core.uploads import DOCUMENT_TYPES, validate_upload

from .models import Document, DocumentCategory


class DocumentForm(forms.ModelForm):
    class Meta:
        model = Document
        fields = ["title", "category", "visibility", "description", "file"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}
        help_texts = {"file": "PDF, Word, Excel, text, or an image."}

    def clean_file(self):
        f = self.cleaned_data.get("file")
        if f and hasattr(f, "content_type"):  # new upload
            validate_upload(f, allowed=DOCUMENT_TYPES)
        return f


class CategoryForm(forms.ModelForm):
    class Meta:
        model = DocumentCategory
        fields = ["name", "sort_order"]


class ShareLinkForm(forms.Form):
    EXPIRY_CHOICES = [("1", "1 day"), ("3", "3 days"), ("7", "1 week"), ("14", "2 weeks"), ("30", "30 days")]

    label = forms.CharField(max_length=150, help_text="Who it's for, e.g. 'Smith closing, 12 Elm St'.")
    documents = forms.ModelMultipleChoiceField(
        queryset=Document.objects.none(), widget=forms.CheckboxSelectMultiple
    )
    expires_in = forms.ChoiceField(choices=EXPIRY_CHOICES, initial="7", label="Link works for")
    max_views = forms.IntegerField(
        required=False,
        min_value=1,
        max_value=1000,
        label="Maximum downloads (optional)",
        help_text="Leave blank for no limit until it expires.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["documents"].queryset = Document.objects.select_related("category")

    def expires_at(self):
        return timezone.now() + timedelta(days=int(self.cleaned_data["expires_in"]))
