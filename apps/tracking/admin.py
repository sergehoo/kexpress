from django.contrib import admin

from apps.tracking.models import (
    DriverDevice,
    GeofenceAlert,
    GeofenceZone,
    OfflineSyncLog,
    RouteDeviationAlert,
    TrackingConsent,
    TripLocationPoint,
    TripRoute,
    TripTrackingSession,
    TripWaypoint,
    VehicleLocation,
)


@admin.register(VehicleLocation)
class VehicleLocationAdmin(admin.ModelAdmin):
    list_display = ["vehicle", "latitude", "longitude", "speed_kmh", "recorded_at"]


@admin.register(TripTrackingSession)
class TripTrackingSessionAdmin(admin.ModelAdmin):
    list_display = ["id", "trip", "status", "started_at", "ended_at", "total_distance_km"]
    list_filter = ["status", "subsidiary"]


@admin.register(GeofenceZone)
class GeofenceZoneAdmin(admin.ModelAdmin):
    list_display = ["name", "zone_type", "subsidiary", "is_active"]
    list_filter = ["zone_type", "is_active", "subsidiary"]


@admin.register(GeofenceAlert)
class GeofenceAlertAdmin(admin.ModelAdmin):
    list_display = ["zone", "vehicle", "event", "severity", "occurred_at"]
    list_filter = ["event", "severity"]


@admin.register(RouteDeviationAlert)
class RouteDeviationAlertAdmin(admin.ModelAdmin):
    list_display = ["trip", "severity", "deviation_m", "occurred_at"]
    list_filter = ["severity"]


@admin.register(DriverDevice)
class DriverDeviceAdmin(admin.ModelAdmin):
    list_display = ["driver", "platform", "is_active", "last_seen"]
    list_filter = ["platform", "is_active"]


admin.site.register(TrackingConsent)
admin.site.register(TripRoute)
admin.site.register(TripWaypoint)
admin.site.register(TripLocationPoint)
admin.site.register(OfflineSyncLog)
