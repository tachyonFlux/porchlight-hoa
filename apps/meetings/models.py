import secrets
import uuid

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone

from apps.core.uploads import private_upload_path


def clock(dt):
    """'7:00 PM' without a leading zero, portably."""
    return dt.strftime("%I:%M %p").lstrip("0")


class Audience(models.TextChoices):
    RESIDENTS = "residents", "Everyone"
    BOARD = "board", "Board only"


class Poll(models.Model):
    """A "when can everyone meet?" poll over a few candidate times."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"

    title = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    audience = models.CharField(max_length=10, choices=Audience.choices, default=Audience.RESIDENTS)
    location = models.CharField(max_length=200, blank=True, help_text="Where the meeting will be, if known.")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    closes_at = models.DateTimeField(
        null=True, blank=True, help_text="Optional voting deadline. Voting stops after this time."
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    chosen_option = models.ForeignKey(
        "PollOption", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse("meetings:poll_detail", args=[self.pk])

    @property
    def accepting_votes(self):
        return self.status == self.Status.OPEN and (self.closes_at is None or self.closes_at > timezone.now())


class PollOption(models.Model):
    poll = models.ForeignKey(Poll, on_delete=models.CASCADE, related_name="options")
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()

    class Meta:
        ordering = ["starts_at", "id"]

    def __str__(self):
        start = timezone.localtime(self.starts_at)
        end = timezone.localtime(self.ends_at)
        return f"{start:%a %b} {start.day}, {clock(start)} – {clock(end)}"


class PollVote(models.Model):
    class Choice(models.TextChoices):
        YES = "yes", "Works for me"
        MAYBE = "maybe", "If needed"
        NO = "no", "Can't make it"

    option = models.ForeignKey(PollOption, on_delete=models.CASCADE, related_name="votes")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="poll_votes")
    choice = models.CharField(max_length=5, choices=Choice.choices)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["option", "user"], name="one_vote_per_option_user")]


class Meeting(models.Model):
    class Kind(models.TextChoices):
        BOARD = "board", "Board meeting"
        ANNUAL = "annual", "Annual meeting"
        GENERAL = "general", "General / community meeting"
        OTHER = "other", "Other"

    title = models.CharField(max_length=150)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.GENERAL)
    audience = models.CharField(
        "who can see it", max_length=10, choices=Audience.choices, default=Audience.RESIDENTS
    )
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    location = models.CharField(max_length=200, blank=True)
    virtual_link = models.URLField("video call link", blank=True)
    agenda = models.TextField(blank=True)
    minutes_text = models.TextField("minutes", blank=True)
    minutes_file = models.FileField(
        "minutes file", upload_to=private_upload_path("minutes"), blank=True, max_length=200
    )
    poll = models.OneToOneField(
        Poll, null=True, blank=True, on_delete=models.SET_NULL, related_name="meeting"
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    sequence = models.PositiveIntegerField(default=0, help_text="Bumped on each change, for calendar apps.")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-starts_at"]

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse("meetings:meeting_detail", args=[self.pk])

    @property
    def is_past(self):
        return self.ends_at < timezone.now()


class CalendarFeedToken(models.Model):
    """Secret URL that lets a calendar app subscribe to a person's meeting feed."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="calendar_feed"
    )
    token = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Calendar feed for {self.user}"

    @classmethod
    def for_user(cls, user, regenerate=False):
        feed, created = cls.objects.get_or_create(user=user, defaults={"token": secrets.token_urlsafe(24)})
        if regenerate and not created:
            feed.token = secrets.token_urlsafe(24)
            feed.save(update_fields=["token", "created_at"])
        return feed
