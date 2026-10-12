import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.db.models import Q
from django.utils import timezone


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class UserManager(BaseUserManager):
    use_in_migrations = True

    def normalize_email(self, email):
        return (email or "").strip().lower()

    def _create_user(self, email, password, **extra):
        email = self.normalize_email(email)
        if not email:
            raise ValueError("An email address is required.")
        user = self.model(email=email, **extra)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email, password=None, **extra):
        extra["is_staff"] = True
        extra["is_superuser"] = True
        return self._create_user(email, password, **extra)

    def get_by_natural_key(self, email):
        return self.get(email__iexact=self.normalize_email(email))


class User(AbstractBaseUser, PermissionsMixin):
    """A person who can sign in. Residents usually have no password and use magic links."""

    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(
        default=False, help_text="Site administrator: can manage board positions and settings."
    )
    date_joined = models.DateTimeField(default=timezone.now)

    objects = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        ordering = ["full_name", "email"]

    def __str__(self):
        return self.full_name or self.email

    def save(self, *args, **kwargs):
        self.email = (self.email or "").strip().lower()
        super().save(*args, **kwargs)

    def get_full_name(self):
        return self.full_name or self.email

    def get_short_name(self):
        return self.full_name.split(" ")[0] if self.full_name else self.email.split("@")[0]

    @property
    def is_admin(self):
        return self.is_active and (self.is_staff or self.is_superuser)

    @property
    def is_board(self):
        """Admins and anyone holding a current board position."""
        if not self.is_active:
            return False
        if self.is_admin:
            return True
        return self.board_positions.current().exists()

    def holds_title(self, *titles):
        return self.is_active and self.board_positions.current().filter(title__in=titles).exists()

    @property
    def has_totp(self):
        return self.totpdevice_set.filter(confirmed=True).exists()


class BoardPositionQuerySet(models.QuerySet):
    def current(self, on=None):
        on = on or timezone.localdate()
        return self.filter(started_on__lte=on).filter(Q(ended_on__isnull=True) | Q(ended_on__gt=on))


class BoardPosition(models.Model):
    class Title(models.TextChoices):
        PRESIDENT = "president", "President"
        VICE_PRESIDENT = "vice_president", "Vice President"
        TREASURER = "treasurer", "Treasurer"
        SECRETARY = "secretary", "Secretary"
        MEMBER = "member", "Board Member"
        OTHER = "other", "Other"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="board_positions"
    )
    title = models.CharField(max_length=20, choices=Title.choices, default=Title.MEMBER)
    custom_title = models.CharField(max_length=60, blank=True)
    started_on = models.DateField(default=timezone.localdate)
    ended_on = models.DateField(null=True, blank=True)

    objects = BoardPositionQuerySet.as_manager()

    class Meta:
        ordering = ["ended_on", "title", "started_on"]

    def __str__(self):
        return f"{self.display_title}: {self.user}"

    @property
    def display_title(self):
        if self.title == self.Title.OTHER and self.custom_title:
            return self.custom_title
        return self.get_title_display()

    @property
    def is_current(self):
        today = timezone.localdate()
        return self.started_on <= today and (self.ended_on is None or self.ended_on > today)


class MagicLinkToken(models.Model):
    """Single-use sign-in link. Only a hash of the token is stored."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="magic_links")
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    requested_ip = models.GenericIPAddressField(null=True, blank=True)

    @classmethod
    def issue(cls, user, ip=None):
        raw = secrets.token_urlsafe(32)
        # Only the newest link works; older unused ones are retired.
        cls.objects.filter(user=user, used_at__isnull=True).update(expires_at=timezone.now())
        token = cls.objects.create(
            user=user,
            token_hash=hash_token(raw),
            expires_at=timezone.now() + timedelta(minutes=settings.MAGIC_LINK_TTL_MINUTES),
            requested_ip=ip,
        )
        return token, raw

    @classmethod
    def find_valid(cls, raw):
        return (
            cls.objects.select_related("user")
            .filter(
                token_hash=hash_token(raw),
                used_at__isnull=True,
                expires_at__gt=timezone.now(),
                user__is_active=True,
            )
            .first()
        )

    def consume(self):
        """Mark used. Returns False if someone else used it first (race-safe)."""
        updated = MagicLinkToken.objects.filter(pk=self.pk, used_at__isnull=True).update(
            used_at=timezone.now()
        )
        return updated == 1


def board_members():
    """Active users who are admins or hold a current board position."""
    current = BoardPosition.objects.current().values("user_id")
    return User.objects.filter(is_active=True).filter(
        Q(pk__in=current) | Q(is_staff=True) | Q(is_superuser=True)
    )
