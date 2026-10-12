from django.contrib import admin

from .models import Deadline


@admin.register(Deadline)
class DeadlineAdmin(admin.ModelAdmin):
    list_display = ["title", "due_date", "category", "completed_at"]
