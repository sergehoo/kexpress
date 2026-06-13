from django.contrib import admin

from apps.vehicles.models import Vehicle, VehicleDocument, VehicleStatusLog


class VehicleDocumentInline(admin.TabularInline):
    model = VehicleDocument
    extra = 0


@admin.register(Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    list_display = ["registration", "brand", "model", "vehicle_type", "status", "subsidiary", "mileage"]
    list_filter = ["status", "vehicle_type", "fuel_type", "subsidiary"]
    search_fields = ["registration", "brand", "model"]
    inlines = [VehicleDocumentInline]


@admin.register(VehicleDocument)
class VehicleDocumentAdmin(admin.ModelAdmin):
    list_display = ["vehicle", "doc_type", "number", "expiry_date"]
    list_filter = ["doc_type"]
    search_fields = ["number", "vehicle__registration"]


@admin.register(VehicleStatusLog)
class VehicleStatusLogAdmin(admin.ModelAdmin):
    list_display = ["vehicle", "previous_status", "new_status", "created_at"]
    list_filter = ["new_status"]
