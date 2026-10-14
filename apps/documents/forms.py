from datetime import timedelta

from django import forms
from django.utils import timezone

from apps.core.uploads import DOCUMENT_TYPES, IMAGE_TYPES, strip_image_metadata, validate_upload

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
            ext = validate_upload(f, allowed=DOCUMENT_TYPES)
            if ext in IMAGE_TYPES:
                f = strip_image_metadata(f)
        return f


class CategoryForm(forms.ModelForm):
    class Meta:
        model = DocumentCategory
        fields = ["name", "sort_order"]

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        clash = DocumentCategory.objects.filter(name__iexact=name).exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError("There's already a category with that name.")
        return name


class ShareLinkForm(forms.Form):
    EXPIRY_CHOICES = [("1", "1 day"), ("3", "3 days"), ("7", "1 week"), ("14", "2 weeks"), ("30", "30 days")]

    label = forms.CharField(
        max_length=150,
        help_text="For your records only; it isn't shown on the shared page. Avoid names and addresses.",
    )
    documents = forms.ModelMultipleChoiceField(
        queryset=Document.objects.none(), widget=forms.CheckboxSelectMultiple
    )
    expires_in = forms.ChoiceField(choices=EXPIRY_CHOICES, initial="7", label="Link works for")
    max_views = forms.IntegerField(
        required=False,
        min_value=1,
        max_value=1000,
        label="Maximum downloads (optional)",
        help_text="Total downloads across all the files. Leave blank for no limit until it expires.",
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["documents"].queryset = Document.objects.select_related("category")
        self.fields["documents"].label_from_instance = lambda d: (
            f"{d.title} (BOARD ONLY)" if d.visibility == Document.Visibility.BOARD else d.title
        )

    def clean_documents(self):
        docs = self.cleaned_data["documents"]
        board_only = any(d.visibility == Document.Visibility.BOARD for d in docs)
        if board_only and not (self.user and self.user.is_admin):
            raise forms.ValidationError(
                "Only an admin can share board-only documents outside the neighborhood."
            )
        return docs

    def expires_at(self):
        return timezone.now() + timedelta(days=int(self.cleaned_data["expires_in"]))
