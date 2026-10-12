"""Porchlight settings. Everything deployment-specific comes from environment variables."""

import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name, default=None):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name, default=""):
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


DEBUG = env_bool("PORCHLIGHT_DEBUG", False)

SECRET_KEY = env("PORCHLIGHT_SECRET_KEY")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "insecure-dev-only-key"  # noqa: S105
    else:
        raise ImproperlyConfigured("Set PORCHLIGHT_SECRET_KEY (see .env.example).")

ALLOWED_HOSTS = env_list("PORCHLIGHT_ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("PORCHLIGHT_CSRF_TRUSTED_ORIGINS")

# Public base URL used to build absolute links in emails and QR codes.
BASE_URL = env("PORCHLIGHT_BASE_URL", "http://localhost:8000").rstrip("/")

# All runtime state (database, uploads, imports) lives under one directory/volume.
DATA_DIR = Path(env("PORCHLIGHT_DATA_DIR", str(BASE_DIR / "data")))
MEDIA_ROOT = DATA_DIR / "media"
MEDIA_URL = "/media/"  # never served directly; private files go through auth-checked views
for _dir in (DATA_DIR, MEDIA_ROOT):
    _dir.mkdir(parents=True, exist_ok=True)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_htmx",
    "django_otp",
    "django_otp.plugins.otp_totp",
    "apps.core",
    "apps.accounts",
    "apps.community",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "apps.core.middleware.SetupRequiredMiddleware",
]

ROOT_URLCONF = "porchlight.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.porchlight",
            ],
        },
    },
]

WSGI_APPLICATION = "porchlight.wsgi.application"

DATABASES = {
    "default": dj_database_url.parse(
        env("DATABASE_URL", f"sqlite:///{DATA_DIR / 'db.sqlite3'}"),
        conn_max_age=60,
    )
}
if DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3":
    # WAL mode + a busy timeout keep SQLite happy with a web container and a scheduler.
    DATABASES["default"]["CONN_MAX_AGE"] = 0
    DATABASES["default"]["OPTIONS"] = {
        "timeout": 20,
        "transaction_mode": "IMMEDIATE",
        "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "core:dashboard"
LOGOUT_REDIRECT_URL = "accounts:login"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("PORCHLIGHT_TIME_ZONE", "America/New_York")
USE_I18N = False
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}

# Sessions: residents stay signed in for a while on their phones.
SESSION_COOKIE_AGE = 60 * 60 * 24 * 30
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

# Reverse proxy / HTTPS (Synology DSM reverse proxy, Caddy, Traefik, nginx...).
BEHIND_PROXY = env_bool("PORCHLIGHT_BEHIND_PROXY", False)
SECURE_COOKIES = env_bool("PORCHLIGHT_SECURE_COOKIES", not DEBUG)
SESSION_COOKIE_SECURE = SECURE_COOKIES
CSRF_COOKIE_SECURE = SECURE_COOKIES
if BEHIND_PROXY:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    USE_X_FORWARDED_HOST = env_bool("PORCHLIGHT_USE_X_FORWARDED_HOST", False)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# Email: SMTP when EMAIL_HOST is set, otherwise print to the console.
EMAIL_HOST = env("EMAIL_HOST", "")
if EMAIL_HOST:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_PORT = int(env("EMAIL_PORT", "587"))
    EMAIL_HOST_USER = env("EMAIL_HOST_USER", "")
    EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")
    EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
    EMAIL_USE_SSL = env_bool("EMAIL_USE_SSL", False)
    EMAIL_TIMEOUT = 20
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "Porchlight <porchlight@localhost>")

# Uploads
MAX_UPLOAD_MB = int(env("PORCHLIGHT_MAX_UPLOAD_MB", "15"))
DATA_UPLOAD_MAX_MEMORY_SIZE = MAX_UPLOAD_MB * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024

# Magic links / claim links
MAGIC_LINK_TTL_MINUTES = int(env("PORCHLIGHT_MAGIC_LINK_TTL_MINUTES", "20"))
CLAIM_LINK_TTL_HOURS = int(env("PORCHLIGHT_CLAIM_LINK_TTL_HOURS", "48"))

# Optional token required to use the first-run setup wizard.
SETUP_TOKEN = env("PORCHLIGHT_SETUP_TOKEN", "")

OTP_TOTP_ISSUER = env("PORCHLIGHT_OTP_ISSUER", "Porchlight")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": env("PORCHLIGHT_LOG_LEVEL", "INFO")},
    "loggers": {"django.db.backends": {"level": "WARNING"}},
}
