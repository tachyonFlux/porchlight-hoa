from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("", include("apps.core.urls")),
    path("", include("apps.accounts.urls")),
    path("", include("apps.community.urls")),
    path("django-admin/", admin.site.urls),
]

handler403 = "apps.core.views.permission_denied"
handler404 = "apps.core.views.page_not_found"
