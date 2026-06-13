from django.contrib import admin

from apps.trips.models import Trip, TripHandover, TripIncident, TripPhoto


class TripHandoverInline(admin.TabularInline):
    model = TripHandover
    extra = 0


class TripIncidentInline(admin.TabularInline):
    model = TripIncident
    extra = 0


class TripPhotoInline(admin.TabularInline):
    model = TripPhoto
    extra = 0


@admin.register(Trip)
class TripAdmin(admin.ModelAdmin):
    list_display = ["id", "destination", "vehicle", "driver", "status", "actual_departure", "actual_return"]
    list_filter = ["status", "subsidiary"]
    search_fields = ["destination", "vehicle__registration"]
    inlines = [TripHandoverInline, TripIncidentInline, TripPhotoInline]
