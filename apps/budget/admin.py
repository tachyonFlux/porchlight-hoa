from django.contrib import admin

from .models import ReserveComponent, Scenario


@admin.register(Scenario)
class ScenarioAdmin(admin.ModelAdmin):
    list_display = ["name", "fiscal_year", "is_adopted", "published_at"]


@admin.register(ReserveComponent)
class ReserveComponentAdmin(admin.ModelAdmin):
    list_display = ["name", "replacement_cost_cents", "useful_life_years", "remaining_life_years"]
