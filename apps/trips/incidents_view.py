"""Endpoint agrégé des incidents (course + chauffeur), scopé par périmètre."""
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.drivers.models import Driver, DriverIncident
from apps.trips.models import Trip, TripIncident


class IncidentsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        trips = Trip.objects.for_user(user)
        drivers = Driver.objects.for_user(user)

        rows = []
        for inc in (
            TripIncident.objects.filter(trip__in=trips)
            .select_related("trip", "trip__vehicle")[:100]
        ):
            rows.append({
                "id": str(inc.id),
                "kind": "trip",
                "kind_display": "Course",
                "severity": inc.severity,
                "severity_display": inc.get_severity_display(),
                "subject": inc.trip.vehicle.registration if inc.trip.vehicle_id else inc.trip.destination,
                "description": inc.description,
                "occurred_at": inc.occurred_at.isoformat() if inc.occurred_at else None,
            })
        for inc in (
            DriverIncident.objects.filter(driver__in=drivers)
            .select_related("driver")[:100]
        ):
            rows.append({
                "id": str(inc.id),
                "kind": "driver",
                "kind_display": "Chauffeur",
                "severity": inc.severity,
                "severity_display": inc.get_severity_display(),
                "subject": inc.driver.full_name,
                "description": inc.description,
                "occurred_at": inc.occurred_at.isoformat() if inc.occurred_at else None,
            })

        rows.sort(key=lambda r: r["occurred_at"] or "", reverse=True)
        return Response({"count": len(rows), "results": rows})
