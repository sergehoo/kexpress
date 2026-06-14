from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tracking.live import compute_positions, record_position, trip_replay, trip_route


class FleetPositionsView(APIView):
    """Positions live (fallback REST / chargement initial ; le WebSocket prend le relais)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        sub = request.query_params.get("subsidiary")
        rows = compute_positions(request.user, sub)
        return Response({"count": len(rows), "results": rows})


class GeofenceZonesView(APIView):
    """Zones géographiques actives du périmètre (affichées sur les cartes)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.tracking.models import GeofenceZone
        from apps.vehicles.models import Vehicle

        user = request.user
        zones = GeofenceZone.objects.filter(is_active=True)
        if not (user.is_superuser or user.has_company_scope):
            zones = zones.filter(subsidiary_id=user.subsidiary_id)
        return Response({
            "results": [
                {
                    "id": str(z.id), "name": z.name, "zone_type": z.zone_type,
                    "zone_type_display": z.get_zone_type_display(), "polygon": z.polygon,
                }
                for z in zones
            ]
        })


class LiveAlertsView(APIView):
    """#3F — Flux des anomalies opérationnelles temps réel pour le centre de contrôle :
    détours, arrêts prolongés, retards, entrées en zone interdite. Scopé, récents d'abord."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from datetime import timedelta

        from django.utils import timezone

        from apps.analytics.scope import scoped
        from apps.tracking.models import GeofenceAlert, RouteDeviationAlert

        qs = scoped(request.user)
        now = timezone.now()
        since = now - timedelta(hours=12)
        trips = qs["trips"]
        vehicles = qs["vehicles"]
        items = []

        # Détours & arrêts prolongés (#3C/#12)
        for a in (
            RouteDeviationAlert.objects.filter(trip__in=trips, occurred_at__gte=since)
            .select_related("trip", "trip__vehicle")[:100]
        ):
            is_stop = a.deviation_m is None
            reg = a.trip.vehicle.registration if a.trip.vehicle_id else "—"
            items.append({
                "kind": "stop" if is_stop else "deviation",
                "severity": a.severity,
                "title": ("Arrêt prolongé" if is_stop else "Hors itinéraire") + f" — {reg}",
                "detail": (
                    "Véhicule à l'arrêt depuis plus de 15 min"
                    if is_stop
                    else f"Écart d'environ {round(float(a.deviation_m) / 1000, 1)} km par rapport à l'itinéraire"
                ),
                "vehicle": reg,
                "trip_id": str(a.trip_id),
                "latitude": str(a.latitude),
                "longitude": str(a.longitude),
                "occurred_at": a.occurred_at.isoformat(),
            })

        # Entrées en zone interdite
        for g in (
            GeofenceAlert.objects.filter(
                vehicle__in=vehicles, zone__zone_type="forbidden", event="enter", occurred_at__gte=since
            ).select_related("vehicle", "zone")[:100]
        ):
            items.append({
                "kind": "forbidden_zone",
                "severity": "critical",
                "title": f"Zone interdite — {g.vehicle.registration}",
                "detail": f"Entrée dans « {g.zone.name} »",
                "vehicle": g.vehicle.registration,
                "trip_id": str(g.trip_id) if g.trip_id else None,
                "latitude": str(g.latitude),
                "longitude": str(g.longitude),
                "occurred_at": g.occurred_at.isoformat(),
            })

        # Retards (courses en cours dont le retour estimé est dépassé)
        for t in trips.filter(status="in_progress").select_related("vehicle", "reservation"):
            er = t.reservation.estimated_return if t.reservation_id else None
            if er and er < now:
                mins = int((now - er).total_seconds() // 60)
                items.append({
                    "kind": "delay",
                    "severity": "critical" if mins >= 60 else "warning",
                    "title": f"Retard — {t.vehicle.registration if t.vehicle_id else '—'}",
                    "detail": f"Retour attendu dépassé de {mins} min — {t.destination}",
                    "vehicle": t.vehicle.registration if t.vehicle_id else "—",
                    "trip_id": str(t.id),
                    "latitude": None,
                    "longitude": None,
                    "occurred_at": er.isoformat(),
                })

        items.sort(key=lambda x: x["occurred_at"], reverse=True)
        counts = {"deviation": 0, "stop": 0, "delay": 0, "forbidden_zone": 0}
        for it in items:
            counts[it["kind"]] = counts.get(it["kind"], 0) + 1
        return Response({"count": len(items), "counts": counts, "results": items[:80]})


class TripRouteView(APIView):
    """Itinéraire prévu (polyline) vs trace réelle GPS d'une course."""

    permission_classes = [IsAuthenticated]

    def get(self, request, trip_id):
        data = trip_route(request.user, trip_id)
        if data is None:
            return Response({"detail": "Course introuvable."}, status=404)
        return Response(data)


class TripReplayView(APIView):
    """Trace GPS complète horodatée d'une course (relecture temporelle)."""

    permission_classes = [IsAuthenticated]

    def get(self, request, trip_id):
        data = trip_replay(request.user, trip_id)
        if data is None:
            return Response({"detail": "Course introuvable."}, status=404)
        return Response(data)


class PositionInputSerializer(serializers.Serializer):
    latitude = serializers.DecimalField(max_digits=9, decimal_places=6)
    longitude = serializers.DecimalField(max_digits=9, decimal_places=6)
    speed_kmh = serializers.DecimalField(max_digits=6, decimal_places=2, required=False, min_value=0)
    heading = serializers.DecimalField(max_digits=5, decimal_places=1, required=False)
    accuracy_m = serializers.DecimalField(max_digits=7, decimal_places=2, required=False, min_value=0)
    # Horodatage GPS d'origine pour les points mis en tampon hors-ligne (rejeu).
    recorded_at = serializers.DateTimeField(required=False)


class TripPositionIngestView(APIView):
    """Ingestion GPS réelle : l'appareil d'un participant pousse sa position pendant la course."""

    permission_classes = [IsAuthenticated]

    def post(self, request, trip_id):
        ser = PositionInputSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        # Un horodatage client trop ancien (> 6 h) ou futur est ignoré → instant serveur.
        from django.utils import timezone

        rec = d.get("recorded_at")
        if rec and (rec > timezone.now() or (timezone.now() - rec).total_seconds() > 21600):
            rec = None
        result = record_position(
            request.user, trip_id,
            latitude=d["latitude"], longitude=d["longitude"],
            speed_kmh=d.get("speed_kmh"), heading=d.get("heading"),
            accuracy_m=d.get("accuracy_m"), recorded_at=rec,
        )
        if not result["ok"]:
            return Response({"detail": result["detail"]}, status=result.get("status", 400))
        return Response(result)
