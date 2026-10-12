from django.contrib import admin

from .models import Meeting, Poll, PollOption


class PollOptionInline(admin.TabularInline):
    model = PollOption
    extra = 0


@admin.register(Poll)
class PollAdmin(admin.ModelAdmin):
    list_display = ["title", "status", "audience", "created_at"]
    inlines = [PollOptionInline]


@admin.register(Meeting)
class MeetingAdmin(admin.ModelAdmin):
    list_display = ["title", "starts_at", "kind", "audience", "cancelled_at"]
