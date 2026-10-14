import secrets

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import hash_token
from apps.core.uploads import private_upload_path


class DocumentCategory(models.Model):
    name = models.CharField(max_length=80, unique=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name_plural = "document categories"

    def __str__(self):
        return self.name


class Document(models.Model):
    class Visibility(models.TextChoices):
        RESIDENTS = "residents", "Residents"
        BOARD = "board", "Board only"

    title = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    category = models.ForeignKey(
        DocumentCategory, null=True, blank=True, on_delete=models.SET_NULL, related_name="documents"
    )
    file = models.FileField(upload_to=private_upload_path("documents"), max_length=200)
    original_name = models.CharField(max_length=200, blank=True)
    file_size = models.PositiveBigIntegerField(default=0)
    visibility = models.CharField(max_length=10, choices=Visibility.choices, default=Visibility.RESIDENTS)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = [
            models.F("category__sort_order").asc(nulls_last=True),
            models.F("category__name").asc(nulls_last=True),
            "title",
        ]

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse("documents:download", args=[self.pk])

    @property
    def extension(self):
        return self.file.name.rsplit(".", 1)[-1].lower() if "." in self.file.name else ""

    @property
    def download_name(self):
        return self.original_name or f"{self.title}.{self.extension}"


class ShareLink(models.Model):
    """A no-login link for an outside party (e.g. a real-estate agent) to fetch chosen documents.

    Only a hash of the token is stored; the full link is shown once, when it's created.
    """

    label = models.CharField(
        max_length=150,
        help_text="For your records only (not shown on the shared page), e.g. 'Closing, March'.",
    )
    token_hash = models.CharField(max_length=64, unique=True)
    documents = models.ManyToManyField(Document, related_name="share_links")
    expires_at = models.DateTimeField()
    max_views = models.PositiveIntegerField(
        "maximum downloads",
        null=True,
        blank=True,
        help_text="Optional: stop working after this many downloads in total (each file counts).",
    )
    allow_board_only = models.BooleanField(
        default=False, help_text="Created by an admin and allowed to include board-only documents."
    )
    view_count = models.PositiveIntegerField(default=0)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_viewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.label

    @staticmethod
    def new_token():
        raw = secrets.token_urlsafe(24)
        return raw, hash_token(raw)

    @classmethod
    def find(cls, raw):
        return cls.objects.filter(token_hash=hash_token(raw)).first()

    @property
    def state(self):
        if self.revoked_at:
            return "revoked"
        if self.expires_at <= timezone.now():
            return "expired"
        if self.max_views is not None and self.view_count >= self.max_views:
            return "used up"
        return "active"

    @property
    def is_active(self):
        return self.state == "active"


class ShareLinkAccess(models.Model):
    link = models.ForeignKey(ShareLink, on_delete=models.CASCADE, related_name="accesses")
    document = models.ForeignKey(Document, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    document_title = models.CharField(max_length=150, blank=True)  # survives the document being deleted
    accessed_at = models.DateTimeField(auto_now_add=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    allowed = models.BooleanField(default=True)

    class Meta:
        ordering = ["-accessed_at"]
