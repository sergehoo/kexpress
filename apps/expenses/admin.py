from django.contrib import admin

from apps.expenses.models import Expense, FleetBudget, FuelLog


@admin.register(FuelLog)
class FuelLogAdmin(admin.ModelAdmin):
    list_display = ["vehicle", "date", "liters", "amount", "price_per_liter", "subsidiary"]
    list_filter = ["subsidiary"]
    search_fields = ["vehicle__registration"]
    date_hierarchy = "date"


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ["label", "category", "amount", "date", "vehicle", "subsidiary"]
    list_filter = ["category", "subsidiary"]
    date_hierarchy = "date"


@admin.register(FleetBudget)
class FleetBudgetAdmin(admin.ModelAdmin):
    list_display = ["label", "subsidiary", "period_start", "period_end", "allocated"]
    list_filter = ["subsidiary"]
