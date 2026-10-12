from django.contrib.admin.apps import AdminConfig


class OTPAdminConfig(AdminConfig):
    """The Django admin, but only usable after TOTP verification."""

    default_site = "django_otp.admin.OTPAdminSite"
