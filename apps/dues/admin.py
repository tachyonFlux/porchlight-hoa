from django.contrib import admin

from .models import BillingPeriod, DuesSettings, Invoice, LedgerEntry, Payment


class ReadOnlyAdmin(admin.ModelAdmin):
    """Ledger data is changed only through the app so every change is audited."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(DuesSettings)
admin.site.register(BillingPeriod, ReadOnlyAdmin)
admin.site.register(Invoice, ReadOnlyAdmin)
admin.site.register(Payment, ReadOnlyAdmin)
admin.site.register(LedgerEntry, ReadOnlyAdmin)
