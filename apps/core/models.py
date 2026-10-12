from django.conf import settings
from django.db import models
from django.utils import timezone


class HOASettings(models.Model):
    """Singleton row holding this neighborhood's configuration."""

    name = models.CharField("HOA name", max_length=150, default="")
    short_name = models.CharField(
        max_length=60, blank=True, help_text="Shown in the header on small screens."
    )
    contact_email = models.EmailField(blank=True, help_text="Where residents can reach the board.")
    mailing_address = models.TextField(
        blank=True, help_text="Where residents mail checks. Printed on invoices."
    )
    setup_complete = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "HOA settings"
        verbose_name_plural = "HOA settings"

    def __str__(self):
        return self.name or "HOA settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def display_name(self):
        return self.name or "Porchlight"


class AuditLog(models.Model):
    """Append-only record of sensitive actions (board financial actions, role changes...)."""

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    action = models.CharField(max_length=80, db_index=True)
    object_type = models.CharField(max_length=80, blank=True)
    object_id = models.CharField(max_length=40, blank=True)
    object_repr = models.CharField(max_length=200, blank=True)
    changes = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.action} {self.object_repr}"


class RateLimitHit(models.Model):
    """Fixed-window counters for rate limiting. Atomic in both SQLite and Postgres."""

    key = models.CharField(max_length=64)
    window_start = models.BigIntegerField()
    count = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["key", "window_start"], name="ratelimit_unique_window")
        ]


class TaskRun(models.Model):
    """One execution of the daily_tasks command."""

    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    ok = models.BooleanField(default=False)
    summary = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-started_at"]
