import os
import tempfile

os.environ.setdefault("PORCHLIGHT_SECRET_KEY", "test-only-secret-key")
os.environ.setdefault("PORCHLIGHT_DATA_DIR", tempfile.mkdtemp(prefix="porchlight-test-"))
os.environ.setdefault("PORCHLIGHT_BASE_URL", "http://testserver")
os.environ.setdefault("DATABASE_URL", "sqlite://:memory:")

from .settings import *  # noqa: E402,F403

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
STORAGES["staticfiles"]["BACKEND"] = "django.contrib.staticfiles.storage.StaticFilesStorage"  # noqa: F405
SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = False
SETUP_TOKEN = ""
SETUP_TOKEN_REQUIRED = False
EMAIL_ASYNC = False
ENABLE_DJANGO_ADMIN = True
STATIC_ROOT = DATA_DIR / "staticfiles"  # noqa: F405
STATIC_ROOT.mkdir(parents=True, exist_ok=True)
