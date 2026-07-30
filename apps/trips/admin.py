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
    # Champs pilotant des INVARIANTS de sûreté : les rendre modifiables à la main permettrait
    # de poser le même groupe de dispatching sur deux courses sans lien (double-booking que la
    # contrainte jugerait alors légitime) ou d'inverser une fenêtre horaire (l'occupation
    # devient négative et la capacité sous-comptée). Ils se modifient par les services.
    readonly_fields = [
        "dispatch_group", "status", "vehicle", "driver",
        "planned_departure_at", "planned_arrival_at",
    ]
