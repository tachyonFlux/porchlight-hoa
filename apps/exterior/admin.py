from django.contrib import admin

from .models import ExteriorRequest


@admin.register(ExteriorRequest)
class ExteriorRequestAdmin(admin.ModelAdmin):
    list_display = ["title", "home", "status", "created_at"]
    list_filter = ["status", "category"]
