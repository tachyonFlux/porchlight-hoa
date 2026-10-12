from django.conf import settings
from django.db import models
from django.urls import reverse

from apps.community.models import Home
from apps.core.uploads import private_upload_path

MAX_PHOTOS = 10


class ExteriorRequest(models.Model):
    """A household asking the board to approve an exterior change (paint, fence, shed...)."""

    class Status(models.TextChoices):
        SUBMITTED = "submitted", "Under review"
        NEEDS_INFO = "needs_info", "More info needed"
        APPROVED = "approved", "Approved"
        DENIED = "denied", "Not approved"
        WITHDRAWN = "withdrawn", "Withdrawn"

    class Category(models.TextChoices):
        PAINT = "paint", "Paint or color"
        FENCE = "fence", "Fence or wall"
        ROOF = "roof", "Roof, siding or windows"
        LANDSCAPING = "landscaping", "Landscaping or trees"
        STRUCTURE = "structure", "Shed, deck, patio or addition"
        OTHER = "other", "Something else"

    OPEN_STATUSES = (Status.SUBMITTED, Status.NEEDS_INFO)
    DECIDED_STATUSES = (Status.APPROVED, Status.DENIED)

    home = models.ForeignKey(Home, on_delete=models.PROTECT, related_name="exterior_requests")
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    title = models.CharField("what are you planning?", max_length=150)
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.OTHER)
    description = models.TextField(
        help_text="Describe the change: materials, colors, size, and where on the property."
    )
    planned_start = models.DateField("planned start (optional)", null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.SUBMITTED)
    decision_note = models.TextField(
        blank=True, help_text="Shown to the household, e.g. conditions of approval."
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.title} ({self.home})"

    def get_absolute_url(self):
        return reverse("exterior:detail", args=[self.pk])

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES


class RequestPhoto(models.Model):
    request = models.ForeignKey(ExteriorRequest, on_delete=models.CASCADE, related_name="photos")
    image = models.FileField(upload_to=private_upload_path("exterior"), max_length=200)
    caption = models.CharField(max_length=150, blank=True)
    board_only = models.BooleanField(default=False, help_text="Attached to an internal board note.")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return self.caption or f"Photo {self.pk}"

    @property
    def is_browser_image(self):
        return not self.image.name.lower().endswith(".heic")


class RequestEvent(models.Model):
    """Append-only history: submissions, comments, and status changes."""

    class Kind(models.TextChoices):
        SUBMITTED = "submitted", "Submitted"
        COMMENT = "comment", "Comment"
        STATUS = "status", "Status changed"
        PHOTOS = "photos", "Photos added"

    request = models.ForeignKey(ExteriorRequest, on_delete=models.CASCADE, related_name="events")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    kind = models.CharField(max_length=10, choices=Kind.choices)
    from_status = models.CharField(max_length=12, blank=True)
    to_status = models.CharField(max_length=12, blank=True)
    note = models.TextField(blank=True)
    board_only = models.BooleanField(
        default=False, help_text="Internal board note, hidden from the household."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.get_kind_display()} on {self.request}"

    @property
    def to_status_label(self):
        return ExteriorRequest.Status(self.to_status).label if self.to_status else ""
