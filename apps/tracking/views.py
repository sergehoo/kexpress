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
