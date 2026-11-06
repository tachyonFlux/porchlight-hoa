import calendar
from datetime import date

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.core.utils import is_int


def parse_reminder_days(value):
    """'30, 7, 1' -> [30, 7, 1]. Raises ValidationError on junk."""
    days = []
    for part in (value or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if not is_int(part) or int(part) > 365:
            raise ValidationError("Use whole numbers of days between 0 and 365, separated by commas.")
        days.append(int(part))
    return sorted(set(days), reverse=True)


def validate_reminder_days(value):
    parse_reminder_days(value)


def add_years(d, years):
    """Same day N years later; Feb 29 becomes Feb 28 in non-leap years."""
    year = d.year + years
    return date(year, d.month, min(d.day, calendar.monthrange(year, d.month)[1]))


class Deadline(models.Model):
    class Category(models.TextChoices):
        FILING = "filing", "State filing"
        TAX = "tax", "Taxes"
        INSURANCE = "insurance", "Insurance"
        GOVERNANCE = "governance", "Meetings & governance"
        FINANCE = "finance", "Budget & finance"
        MAINTENANCE = "maintenance", "Maintenance & contracts"
        OTHER = "other", "Other"

    class Recurrence(models.TextChoices):
        NONE = "none", "Doesn't repeat"
        YEARLY = "yearly", "Every year"

    title = models.CharField(max_length=150)
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.OTHER)
    description = models.TextField(
        blank=True, help_text="What needs to happen, links, account numbers to use, etc."
    )
    due_date = models.DateField()
    recurrence = models.CharField(max_length=10, choices=Recurrence.choices, default=Recurrence.YEARLY)
    remind_days = models.CharField(
        "remind this many days before",
        max_length=60,
        default="30, 7, 1",
        validators=[validate_reminder_days],
        help_text="Comma-separated, e.g. 30, 7, 1. Reminders go to the owner (or the whole board).",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Who's responsible. Leave blank to remind the whole board.",
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    completion_note = models.CharField(max_length=300, blank=True)
    next_occurrence = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="previous_occurrence"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["completed_at", "due_date", "title"]

    def __str__(self):
        return f"{self.title} ({self.due_date})"

    @property
    def is_done(self):
        return self.completed_at is not None

    @property
    def days_left(self):
        return (self.due_date - timezone.localdate()).days

    @property
    def urgency(self):
        if self.is_done:
            return "done"
        days = self.days_left
        if days < 0:
            return "overdue"
        if days <= 14:
            return "soon"
        return "later"

    @property
    def reminder_days(self):
        return parse_reminder_days(self.remind_days)


class DeadlineReminder(models.Model):
    """One row per reminder sent. days_before = -1 marks the 'overdue' alert."""

    OVERDUE = -1

    deadline = models.ForeignKey(Deadline, on_delete=models.CASCADE, related_name="reminders")
    days_before = models.SmallIntegerField()
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["deadline", "days_before"], name="one_reminder_per_deadline_day")
        ]
