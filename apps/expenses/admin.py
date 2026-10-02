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
    list_display = ["label", "category", "amount", "date", "status", "vehicle", "subsidiary"]
    list_filter = ["category", "status", "subsidiary"]
    date_hierarchy = "date"
    #: Le circuit (statuts, validation, paiement, reprise) ne s'écrit que par ses actions
    #: tracées (`apps.expenses.workflow`), jamais par un formulaire d'administration.
    readonly_fields = ["status", "submitted_at", "validated_at", "validated_by", "paid_at", "paid_by",
                       "payment_reference", "payment_method", "accounting_reference",
                       "accounting_exported_at", "original_category", "reconciled_at",
                       "reconciled_by", "reconciliation", "receipt_required"]


@admin.register(FleetBudget)
class FleetBudgetAdmin(admin.ModelAdmin):
    list_display = ["label", "subsidiary", "period_start", "period_end", "allocated"]
    list_filter = ["subsidiary"]
