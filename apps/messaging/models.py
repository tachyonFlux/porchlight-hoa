from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone

from apps.community.models import Home


class Announcement(models.Model):
    class Audience(models.TextChoices):
        RESIDENTS = "residents", "Everyone"
        BOARD = "board", "Board only"

    title = models.CharField(max_length=150)
    body = models.TextField()
    audience = models.CharField(max_length=10, choices=Audience.choices, default=Audience.RESIDENTS)
    pinned = models.BooleanField(default=False, help_text="Keep at the top of the list and the home page.")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    emailed_at = models.DateTimeField(null=True, blank=True)
    email_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-pinned", "-created_at"]

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse("messaging:announcement_detail", args=[self.pk])


class CourtesyNotice(models.Model):
    """A friendly note from the board to one household (e.g. trash cans left out)."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        RESOLVED = "resolved", "Resolved"

    home = models.ForeignKey(Home, on_delete=models.PROTECT, related_name="courtesy_notices")
    subject = models.CharField(max_length=150)
    body = models.TextField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    emailed_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    resolution_note = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.subject} ({self.home})"

    def get_absolute_url(self):
        return reverse("messaging:notice_detail", args=[self.pk])


class NoticeReply(models.Model):
    notice = models.ForeignKey(CourtesyNotice, on_delete=models.CASCADE, related_name="replies")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    body = models.TextField(max_length=4000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        verbose_name_plural = "notice replies"


class BoardThread(models.Model):
    subject = models.CharField(max_length=150)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_activity_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-last_activity_at"]

    def __str__(self):
        return self.subject

    def get_absolute_url(self):
        return reverse("messaging:thread_detail", args=[self.pk])


class BoardMessage(models.Model):
    thread = models.ForeignKey(BoardThread, on_delete=models.CASCADE, related_name="messages")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    body = models.TextField(max_length=10000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


class BoardThreadRead(models.Model):
    """When each board member last opened each thread, for unread markers."""

    thread = models.ForeignKey(BoardThread, on_delete=models.CASCADE, related_name="reads")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    last_read_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["thread", "user"], name="one_read_marker_per_thread_user")
        ]
