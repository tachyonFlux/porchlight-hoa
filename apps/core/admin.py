from django.contrib import admin

from .models import AuditLog, HOASettings, TaskRun


@admin.register(HOASettings)
class HOASettingsAdmin(admin.ModelAdmin):
    list_display = ["name", "setup_complete", "updated_at"]


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ["created_at", "actor", "action", "object_repr"]
    list_filter = ["action"]
    readonly_fields = [f.name for f in AuditLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(TaskRun)
class TaskRunAdmin(admin.ModelAdmin):
    list_display = ["started_at", "finished_at", "ok"]
