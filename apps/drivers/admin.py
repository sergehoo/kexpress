from django.contrib import admin

from apps.drivers.models import (
    Driver,
    DriverAvailability,
    DriverEvaluation,
    DriverIncident,
)


@admin.register(Driver)
class DriverAdmin(admin.ModelAdmin):
    list_display = ["full_name", "subsidiary", "license_category", "license_expiry", "is_available", "rating"]
    list_filter = ["subsidiary", "is_available", "license_category"]
    search_fields = ["first_name", "last_name", "license_number", "phone"]


@admin.register(DriverAvailability)
class DriverAvailabilityAdmin(admin.ModelAdmin):
    list_display = ["driver", "start", "end", "is_available"]
    list_filter = ["is_available"]


@admin.register(DriverEvaluation)
class DriverEvaluationAdmin(admin.ModelAdmin):
    list_display = ["driver", "score", "evaluator", "created_at"]


@admin.register(DriverIncident)
class DriverIncidentAdmin(admin.ModelAdmin):
    list_display = ["driver", "severity", "occurred_at"]
    list_filter = ["severity"]
