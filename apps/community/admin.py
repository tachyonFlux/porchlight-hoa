from django.contrib import admin

from .models import Home, HomeDetail, HomeInvite, Residency


class ResidencyInline(admin.TabularInline):
    model = Residency
    extra = 0
    autocomplete_fields = ["user"]


class HomeDetailInline(admin.TabularInline):
    model = HomeDetail
    extra = 0


@admin.register(Home)
class HomeAdmin(admin.ModelAdmin):
    list_display = ["address", "lot_number", "is_active"]
    search_fields = ["address", "lot_number"]
    inlines = [ResidencyInline, HomeDetailInline]


@admin.register(HomeInvite)
class HomeInviteAdmin(admin.ModelAdmin):
    list_display = ["home", "code", "created_at", "revoked_at", "use_count"]
