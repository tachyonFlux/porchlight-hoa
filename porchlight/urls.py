from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from apps.core.ratelimit import rate_limit

urlpatterns = [
    path("", include("apps.core.urls")),
    path("", include("apps.accounts.urls")),
    path("", include("apps.community.urls")),
    path("", include("apps.dues.urls")),
    path("", include("apps.meetings.urls")),
    path("", include("apps.exterior.urls")),
    path("", include("apps.messaging.urls")),
    path("", include("apps.compliance.urls")),
    path("", include("apps.documents.urls")),
    path("", include("apps.finance.urls")),
    path("", include("apps.budget.urls")),
    path("", include("apps.tax.urls")),
]

if settings.ENABLE_DJANGO_ADMIN:
    # admin.site is a django_otp OTPAdminSite (see porchlight/apps.py): TOTP required.
    admin.site.login = rate_limit("admin-login", limit=10, period=900)(admin.site.login)
    urlpatterns.append(path("django-admin/", admin.site.urls))

handler403 = "apps.core.views.permission_denied"
handler404 = "apps.core.views.page_not_found"
