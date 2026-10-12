from django.contrib import admin

from .models import Announcement, CourtesyNotice


@admin.register(Announcement)
class AnnouncementAdmin(admin.ModelAdmin):
    list_display = ["title", "audience", "pinned", "created_at"]


@admin.register(CourtesyNotice)
class CourtesyNoticeAdmin(admin.ModelAdmin):
    list_display = ["subject", "home", "status", "created_at"]
