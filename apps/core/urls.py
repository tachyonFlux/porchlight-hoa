from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("setup/", views.setup, name="setup"),
    path("healthz", views.healthz, name="healthz"),
    path("board/settings/", views.hoa_settings, name="settings"),
    path("board/audit/", views.audit_log, name="audit_log"),
]
