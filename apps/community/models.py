import re
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import hash_token

INVITE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L


def normalize_address(value):
    return re.sub(r"\s+", " ", (value or "").strip())


def normalize_invite_code(value):
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


class Home(models.Model):
    address = models.CharField(max_length=200, unique=True)
    lot_number = models.CharField("lot #", max_length=40, blank=True)
    notes = models.TextField(blank=True, help_text="Board-only notes.")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["address"]

    def __str__(self):
        return self.address

    def save(self, *args, **kwargs):
        self.address = normalize_address(self.address)
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("community:home_detail", args=[self.pk])

    def current_residencies(self):
        return self.residencies.current().select_related("user")

    def current_residents(self):
        return [r.user for r in self.current_residencies()]


class ResidencyQuerySet(models.QuerySet):
    def current(self):
        return self.filter(ended_on__isnull=True, user__is_active=True)


class Residency(models.Model):
    class Relation(models.TextChoices):
        OWNER = "owner", "Owner"
        TENANT = "tenant", "Tenant"
        HOUSEHOLD = "household", "Household member"

    class Source(models.TextChoices):
        BOARD = "board", "Added by board"
        IMPORT = "import", "CSV import"
        CLAIM = "claim", "Claimed with invite"

    home = models.ForeignKey(Home, on_delete=models.CASCADE, related_name="residencies")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="residencies")
    relation = models.CharField(max_length=20, choices=Relation.choices, default=Relation.OWNER)
    is_primary_contact = models.BooleanField(default=False)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.BOARD)
    started_on = models.DateField(default=timezone.localdate)
    ended_on = models.DateField(null=True, blank=True)

    objects = ResidencyQuerySet.as_manager()

    class Meta:
        ordering = ["home__address", "-is_primary_contact", "user__full_name"]
        verbose_name_plural = "residencies"
        constraints = [
            models.UniqueConstraint(
                fields=["home", "user"],
                condition=Q(ended_on__isnull=True),
                name="one_current_residency_per_home_user",
            )
        ]

    def __str__(self):
        return f"{self.user} @ {self.home}"


class HomeInviteQuerySet(models.QuerySet):
    def valid(self):
        return self.filter(revoked_at__isnull=True, home__is_active=True).filter(
            Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now())
        )


class HomeInvite(models.Model):
    """Code printed on a letter or shared by the board; lets residents attach to a home."""

    home = models.ForeignKey(Home, on_delete=models.CASCADE, related_name="invites")
    code = models.CharField(max_length=16, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    use_count = models.PositiveIntegerField(default=0)

    objects = HomeInviteQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.display_code} ({self.home})"

    @staticmethod
    def generate_code():
        return "".join(secrets.choice(INVITE_ALPHABET) for _ in range(8))

    @classmethod
    def active_for(cls, home, created_by=None):
        """Return the home's current invite, creating one if needed."""
        invite = cls.objects.valid().filter(home=home).first()
        if invite is None:
            invite = cls.objects.create(home=home, code=cls.generate_code(), created_by=created_by)
        return invite

    @classmethod
    def lookup(cls, raw_code):
        code = normalize_invite_code(raw_code)
        if len(code) != 8:
            return None
        return cls.objects.valid().select_related("home").filter(code=code).first()

    @property
    def display_code(self):
        return f"{self.code[:4]}-{self.code[4:]}"

    @property
    def is_valid(self):
        return (
            self.revoked_at is None
            and self.home.is_active
            and (self.expires_at is None or self.expires_at > timezone.now())
        )

    def get_claim_url(self):
        return reverse("community:claim_code", args=[self.code])

    def revoke(self):
        self.revoked_at = timezone.now()
        self.save(update_fields=["revoked_at"])


class ClaimToken(models.Model):
    """Email confirmation for a claim. Only a hash of the token is stored."""

    invite = models.ForeignKey(HomeInvite, on_delete=models.CASCADE, related_name="claims")
    email = models.EmailField()
    full_name = models.CharField(max_length=150, blank=True)
    relation = models.CharField(
        max_length=20, choices=Residency.Relation.choices, default=Residency.Relation.OWNER
    )
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    requested_ip = models.GenericIPAddressField(null=True, blank=True)

    @classmethod
    def issue(cls, invite, email, full_name="", relation=Residency.Relation.OWNER, ip=None):
        raw = secrets.token_urlsafe(32)
        token = cls.objects.create(
            invite=invite,
            email=email.strip().lower(),
            full_name=full_name,
            relation=relation,
            token_hash=hash_token(raw),
            expires_at=timezone.now() + timedelta(hours=settings.CLAIM_LINK_TTL_HOURS),
            requested_ip=ip,
        )
        return token, raw

    @classmethod
    def find_valid(cls, raw):
        token = (
            cls.objects.select_related("invite__home")
            .filter(token_hash=hash_token(raw), used_at__isnull=True, expires_at__gt=timezone.now())
            .first()
        )
        if token and token.invite.is_valid:
            return token
        return None

    def consume(self):
        return ClaimToken.objects.filter(pk=self.pk, used_at__isnull=True).update(used_at=timezone.now()) == 1


class HomeDetail(models.Model):
    """Free-form key/value facts about a home, with a visibility level."""

    class Visibility(models.TextChoices):
        HOUSEHOLD = "household", "Household only"
        NEIGHBORS = "neighbors", "Neighbors"
        BOARD = "board", "Board only"

    home = models.ForeignKey(Home, on_delete=models.CASCADE, related_name="details")
    key = models.CharField("label", max_length=80)
    value = models.TextField(max_length=2000)
    visibility = models.CharField(max_length=10, choices=Visibility.choices, default=Visibility.HOUSEHOLD)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["key"]
        constraints = [models.UniqueConstraint(fields=["home", "key"], name="unique_home_detail_key")]

    def __str__(self):
        return f"{self.home}: {self.key}"
