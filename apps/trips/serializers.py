from rest_framework import serializers

from apps.core.enums import TripStatus
from apps.trips.models import Trip, TripIncident


class TripIncidentSerializer(serializers.ModelSerializer):
    severity_display = serializers.CharField(source="get_severity_display", read_only=True)

    class Meta:
        model = TripIncident
        fields = ["id", "occurred_at", "severity", "severity_display", "description"]


class TripSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    vehicle_registration = serializers.CharField(source="vehicle.registration", read_only=True)
    vehicle_label = serializers.SerializerMethodField()
    driver_name = serializers.CharField(source="driver.full_name", read_only=True, default=None)
    requester_name = serializers.CharField(source="requester.get_full_name", read_only=True, default=None)
    subsidiary_name = serializers.CharField(source="subsidiary.name", read_only=True)
    incidents = TripIncidentSerializer(many=True, read_only=True)
    # Fuel Intelligence : estimé pour tous ; coûts réservés aux gestionnaires.
    estimated_fuel_l = serializers.SerializerMethodField()
    fuel_intel = serializers.SerializerMethodField()
    # #9 — l'utilisateur courant peut-il démarrer cette course ? (gate du bouton)
    can_start = serializers.SerializerMethodField()

    def get_vehicle_label(self, obj):
        v = obj.vehicle
        return f"{v.brand} {v.model}".strip() if v else None

    def get_can_start(self, obj):
        if obj.status != TripStatus.SCHEDULED:
            return False
        user = getattr(self.context.get("request"), "user", None)
        if not user:
            return False
        from apps.trips.services import can_start_trip

        return can_start_trip(obj, user)

    class Meta:
        model = Trip
        fields = [
            "id", "reservation", "requester", "requester_name",
            "subsidiary", "subsidiary_name",
            "vehicle", "vehicle_registration", "vehicle_label",
            "driver", "driver_name", "destination",
            "status", "status_display",
            "actual_departure", "actual_return", "start_mileage", "end_mileage",
            "distance_km", "fuel_consumed", "observations",
            "estimated_fuel_l", "fuel_intel", "can_start", "incidents",
            "created_at", "updated_at",
        ]
        read_only_fields = fields  # courses gérées via réservations + actions

    def get_estimated_fuel_l(self, obj):
        route = getattr(obj, "route", None)
        return float(route.estimated_fuel_l) if (route and route.estimated_fuel_l is not None) else None

    def get_fuel_intel(self, obj):
        """Analyses carburant (écart, coût, score) — gestionnaires uniquement."""
        request = self.context.get("request")
        user = getattr(request, "user", None)
        from apps.fuelintel.access import can_see_costs

        if not (user and can_see_costs(user)):
            return None

        from decimal import Decimal

        from apps.fuelintel.engine import fuel_cost

        route = getattr(obj, "route", None)
        estimated = route.estimated_fuel_l if (route and route.estimated_fuel_l is not None) else None
        real = obj.fuel_consumed
        gap_pct = None
        if estimated and real and estimated > 0:
            gap_pct = round(float((Decimal(real) - estimated) / estimated * 100), 1)
        # Score d'efficacité énergétique : 100 = conforme à l'estimation, pénalisé par l'écart.
        score = None
        if gap_pct is not None:
            score = max(0, min(100, round(100 - abs(gap_pct))))
        cost = fuel_cost(Decimal(real), obj.vehicle.fuel_type) if real else None
        return {
            "estimated_l": float(estimated) if estimated is not None else None,
            "real_l": float(real) if real is not None else None,
            "gap_pct": gap_pct,
            "efficiency_score": score,
            "fuel_cost": float(cost["cost"]) if cost else None,
            "fuel_price": float(cost["price"]) if cost else None,
            "fuel_price_date": cost["price_date"].isoformat() if (cost and cost["price_date"]) else None,
        }


class StartTripInputSerializer(serializers.Serializer):
    start_mileage = serializers.IntegerField(required=False, min_value=0)


class EndTripInputSerializer(serializers.Serializer):
    # Optionnel : sans valeur, le backend estime depuis la progression de l'itinéraire.
    end_mileage = serializers.IntegerField(min_value=0, required=False)
    fuel_consumed = serializers.DecimalField(
        max_digits=7, decimal_places=2, required=False, min_value=0
    )
