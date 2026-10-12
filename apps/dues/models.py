import secrets
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.urls import reverse

from apps.community.models import Home

from .periods import FREQUENCY_MONTHS


class DuesSettings(models.Model):
    """Singleton with this HOA's dues rules."""

    class Frequency(models.TextChoices):
        ANNUAL = "annual", "Annually"
        SEMIANNUAL = "semiannual", "Twice a year"
        QUARTERLY = "quarterly", "Quarterly"
        MONTHLY = "monthly", "Monthly"

    class LateFeeType(models.TextChoices):
        NONE = "none", "No late fee"
        FLAT = "flat", "Flat amount"
        PERCENT = "percent", "Percent of the unpaid amount"

    auto_invoicing = models.BooleanField(
        default=False,
        help_text="Create invoices automatically each period (run by the daily scheduled task).",
    )
    amount_cents = models.BigIntegerField("dues per period", default=0)
    frequency = models.CharField(max_length=12, choices=Frequency.choices, default=Frequency.ANNUAL)
    anchor_month = models.PositiveSmallIntegerField(
        "first billing month",
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(12)],
        help_text="The month the dues year starts (1 = January).",
    )
    due_day = models.PositiveSmallIntegerField(
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(31)],
        help_text="Day of the period's first month when dues are due.",
    )
    invoice_lead_days = models.PositiveSmallIntegerField(
        default=30, help_text="How many days before the due date invoices are created."
    )
    billing_start_date = models.DateField(
        null=True,
        blank=True,
        help_text="Automatic invoices start with the first period beginning on or after this date.",
    )
    grace_days = models.PositiveSmallIntegerField(
        default=30, help_text="Days after the due date before a payment counts as late."
    )
    late_fee_type = models.CharField(max_length=10, choices=LateFeeType.choices, default=LateFeeType.NONE)
    late_fee_flat_cents = models.BigIntegerField("flat late fee", default=0)
    late_fee_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0"),
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    payable_to = models.CharField(max_length=150, blank=True, help_text="Who checks should be made out to.")
    payment_instructions = models.TextField(
        blank=True, help_text="Extra notes printed on invoices, e.g. 'Write your address on the memo line.'"
    )
    email_new_invoices = models.BooleanField(default=True)
    reminder_days_before = models.PositiveSmallIntegerField(
        default=14, help_text="Send a friendly reminder this many days before the due date (0 = off)."
    )
    overdue_reminder_every_days = models.PositiveSmallIntegerField(
        default=30, help_text="After the grace period, remind every N days while unpaid (0 = off)."
    )
    overdue_reminder_max = models.PositiveSmallIntegerField(
        default=3, help_text="Maximum overdue reminders per invoice."
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "dues settings"
        verbose_name_plural = "dues settings"

    def __str__(self):
        return "Dues settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def periods_per_year(self):
        return 12 // FREQUENCY_MONTHS[self.frequency]

    @property
    def annual_amount_cents(self):
        return self.amount_cents * self.periods_per_year


class BillingPeriod(models.Model):
    start = models.DateField(unique=True)
    end = models.DateField()
    due_date = models.DateField()
    label = models.CharField(max_length=80)
    amount_cents = models.BigIntegerField(help_text="Dues per home for this period.")
    grace_days = models.PositiveSmallIntegerField(default=30)
    # Late-fee terms are snapshotted too, so invoices are charged what they said.
    late_fee_type = models.CharField(max_length=10, choices=DuesSettings.LateFeeType.choices, default="none")
    late_fee_flat_cents = models.BigIntegerField(default=0)
    late_fee_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-start"]

    def __str__(self):
        return self.label

    @property
    def late_after(self):
        """Last day a payment counts as on time."""
        return self.due_date + timedelta(days=self.grace_days)

    def overlaps(self):
        return BillingPeriod.objects.filter(start__lte=self.end, end__gte=self.start).exclude(pk=self.pk)


PUBLIC_LINK_DAYS = 90


def _invoice_token():
    return secrets.token_urlsafe(18)


class Invoice(models.Model):
    home = models.ForeignKey(Home, on_delete=models.PROTECT, related_name="invoices")
    period = models.ForeignKey(BillingPeriod, on_delete=models.PROTECT, related_name="invoices")
    amount_cents = models.BigIntegerField()
    issued_on = models.DateField()
    public_token = models.CharField(max_length=40, unique=True, default=_invoice_token)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-period__start", "home__address"]
        constraints = [models.UniqueConstraint(fields=["home", "period"], name="one_invoice_per_home_period")]

    def __str__(self):
        return f"{self.period.label}: {self.home}"

    def get_absolute_url(self):
        return reverse("dues:invoice_detail", args=[self.pk])

    @property
    def number(self):
        return f"{self.period.start:%Y%m}-{self.pk:05d}"

    def get_public_url(self):
        return reverse("dues:invoice_public", args=[self.public_token])

    @property
    def public_link_expires(self):
        """The QR/email link stops working 90 days after the period ends."""
        return self.period.end + timedelta(days=PUBLIC_LINK_DAYS)


class PaymentMethod(models.TextChoices):
    """How money arrived. Only manual methods in v1; online methods (e.g. ACH via a
    processor) would be added here and recorded through the same service layer."""

    CHECK = "check", "Check"
    CASH = "cash", "Cash"
    OTHER = "other", "Other"


class Payment(models.Model):
    home = models.ForeignKey(Home, on_delete=models.PROTECT, related_name="payments")
    amount_cents = models.BigIntegerField()
    method = models.CharField(max_length=12, choices=PaymentMethod.choices, default=PaymentMethod.CHECK)
    check_number = models.CharField(max_length=30, blank=True)
    received_on = models.DateField()
    note = models.CharField(max_length=200, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-received_on", "-id"]
        constraints = [models.CheckConstraint(condition=Q(amount_cents__gt=0), name="payment_positive")]

    def __str__(self):
        label = self.get_method_display()
        if self.check_number:
            label += f" #{self.check_number}"
        d = self.received_on
        return f"{label} from {self.home} on {d:%b} {d.day}, {d.year}"


class LedgerEntry(models.Model):
    """Per-home ledger line. Positive amounts are charges, negative amounts are credits.

    Entries are never edited or deleted; corrections are made with new entries.
    """

    class Kind(models.TextChoices):
        INVOICE = "invoice", "Dues invoice"
        LATE_FEE = "late_fee", "Late fee"
        LATE_FEE_WAIVER = "late_fee_waiver", "Late fee waived"
        PAYMENT = "payment", "Payment"
        PAYMENT_VOID = "payment_void", "Payment voided"
        CREDIT = "credit", "Credit"
        CHARGE = "charge", "Charge"

    home = models.ForeignKey(Home, on_delete=models.PROTECT, related_name="ledger_entries")
    date = models.DateField()
    kind = models.CharField(max_length=20, choices=Kind.choices)
    amount_cents = models.BigIntegerField()
    memo = models.CharField(max_length=200, blank=True)
    invoice = models.ForeignKey(
        Invoice, null=True, blank=True, on_delete=models.PROTECT, related_name="entries"
    )
    payment = models.ForeignKey(
        Payment, null=True, blank=True, on_delete=models.PROTECT, related_name="entries"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date", "id"]
        verbose_name_plural = "ledger entries"
        constraints = [
            # These make invoice generation, late fees and voids idempotent.
            models.UniqueConstraint(
                fields=["invoice", "kind"],
                condition=Q(kind__in=["invoice", "late_fee", "late_fee_waiver"]),
                name="one_entry_per_invoice_kind",
            ),
            models.UniqueConstraint(
                fields=["payment", "kind"],
                condition=Q(kind__in=["payment", "payment_void"]),
                name="one_entry_per_payment_kind",
            ),
        ]

    def __str__(self):
        return f"{self.date} {self.get_kind_display()} {self.amount_cents}"

    @property
    def is_charge(self):
        return self.amount_cents > 0


class ReminderLog(models.Model):
    """One row per reminder email sent, so reminders are never duplicated."""

    class Kind(models.TextChoices):
        ISSUED = "issued", "New invoice"
        UPCOMING = "upcoming", "Due soon"
        OVERDUE = "overdue", "Overdue"

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="reminders")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    sequence = models.PositiveSmallIntegerField(default=0)
    sent_at = models.DateTimeField(auto_now_add=True)
    recipients = models.TextField(blank=True)

    class Meta:
        ordering = ["-sent_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["invoice", "kind", "sequence"], name="one_reminder_per_kind_sequence"
            )
        ]
