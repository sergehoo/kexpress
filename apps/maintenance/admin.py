from django.contrib import admin

from apps.maintenance.models import (
    MaintenanceRecord,
    MaintenanceSchedule,
    MaintenanceType,
)


@admin.register(MaintenanceType)
class MaintenanceTypeAdmin(admin.ModelAdmin):
    list_display = ["name", "interval_km", "interval_days"]
    search_fields = ["name"]


@admin.register(MaintenanceSchedule)
class MaintenanceScheduleAdmin(admin.ModelAdmin):
    list_display = ["vehicle", "maintenance_type", "due_date", "due_mileage", "is_active"]
    list_filter = ["is_active", "maintenance_type"]


@admin.register(MaintenanceRecord)
class MaintenanceRecordAdmin(admin.ModelAdmin):
    list_display = ["vehicle", "maintenance_type", "status", "scheduled_date", "performed_date", "cost"]
    list_filter = ["status", "subsidiary", "maintenance_type"]
    search_fields = ["vehicle__registration", "provider"]
