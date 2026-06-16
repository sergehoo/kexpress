from datetime import timedelta

from django.db.models import Count, Sum
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.organizations.models import Company, Subsidiary
from apps.organizations.serializers import SubsidiarySerializer

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

#: Statuts de réservation comptés comme « validés » / « en attente ».
_RES_VALIDATED = ("approved", "vehicle_assigned", "driver_assigned", "in_progress", "completed", "closed")
_RES_PENDING = ("submitted", "pending_manager", "pending_fleet")


def subsidiary_stats(sub, user) -> dict:
    """Statistiques d'activité d'une filiale (parc, chauffeurs, demandes, courses,
    coûts, conformité). Les montants financiers sont masqués si l'utilisateur n'a pas
    le droit de voir les coûts (can_see_costs) — seuls litres/compteurs restent."""
    from apps.drivers.models import Driver
    from apps.expenses.models import Expense, FuelLog
    from apps.fuelintel.access import can_see_costs
    from apps.maintenance.models import MaintenanceRecord
    from apps.reservations.models import Reservation
    from apps.trips.models import Trip
    from apps.vehicles.models import InsurancePolicy, TechnicalInspection, Vehicle

    today = timezone.localdate()
    month_start = today.replace(day=1)
    soon = today + timedelta(days=30)

    veh = Vehicle.objects.filter(subsidiary=sub)
    by_status = {r["status"]: r["n"] for r in veh.values("status").annotate(n=Count("id"))}
    vehicles = {
        "total": veh.count(),
        "available": by_status.get("available", 0),
        "on_trip": by_status.get("on_trip", 0),
        "maintenance": by_status.get("maintenance", 0),
        "out_of_service": by_status.get("out_of_service", 0),
    }

    drv = Driver.objects.filter(subsidiary=sub)
    drivers = {"total": drv.count(), "available": drv.filter(is_available=True).count()}

    res = Reservation.objects.filter(subsidiary=sub)
    res_month = res.filter(trip_date__gte=month_start)
    by_res = {r["status"]: r["n"] for r in res_month.values("status").annotate(n=Count("id"))}
    reservations = {
        "today": res.filter(trip_date=today).count(),
        "month": res_month.count(),
        "validated": sum(by_res.get(s, 0) for s in _RES_VALIDATED),
        "pending": sum(by_res.get(s, 0) for s in _RES_PENDING),
        "rejected": by_res.get("rejected", 0),
    }

    trips = Trip.objects.filter(subsidiary=sub)
    trips_stats = {
        "in_progress": trips.filter(status="in_progress").count(),
        "completed": trips.filter(status__in=["returned", "closed"]).count(),
        "total": trips.count(),
        "distance_km": float(trips.aggregate(s=Sum("distance_km"))["s"] or 0),
    }

    fuel = FuelLog.objects.filter(subsidiary=sub, date__gte=month_start)
    costs = {"fuel_liters": float(fuel.aggregate(s=Sum("liters"))["s"] or 0), "can_see_costs": can_see_costs(user)}
    if costs["can_see_costs"]:
        f = float(fuel.aggregate(s=Sum("amount"))["s"] or 0)
        m = float(MaintenanceRecord.objects.filter(subsidiary=sub, scheduled_date__gte=month_start).aggregate(s=Sum("cost"))["s"] or 0)
        e = float(Expense.objects.filter(subsidiary=sub, date__gte=month_start).aggregate(s=Sum("amount"))["s"] or 0)
        costs.update(fuel=f, maintenance=m, expenses=e, total=f + m + e)

    alerts = {
        "immobilized": vehicles["maintenance"] + vehicles["out_of_service"],
        "insurance_expiring": InsurancePolicy.objects.filter(vehicle__subsidiary=sub, expiry_date__lte=soon).count(),
        "inspection_expiring": TechnicalInspection.objects.filter(vehicle__subsidiary=sub, next_date__lte=soon).count(),
    }

    return {"period": "month", "vehicles": vehicles, "drivers": drivers,
            "reservations": reservations, "trips": trips_stats, "costs": costs, "alerts": alerts}


class SubsidiaryViewSet(viewsets.ModelViewSet):
    """Filiales : lecture selon périmètre ; écriture réservée au périmètre entreprise."""

    serializer_class = SubsidiarySerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = Subsidiary.objects.select_related("company")
        user = self.request.user
        if user.is_superuser or user.has_company_scope:
            return qs
        if user.subsidiary_id:
            return qs.filter(pk=user.subsidiary_id)
        return qs.none()

    def list(self, request, *args, **kwargs):
        """Liste enrichie de compteurs compacts (parc, chauffeurs, courses, demandes)
        pour les cartes — calculés en quelques requêtes groupées (pas de N+1)."""
        response = super().list(request, *args, **kwargs)
        data = response.data
        rows = data["results"] if isinstance(data, dict) and "results" in data else data
        ids = [str(r["id"]) for r in rows]
        if ids:
            from apps.drivers.models import Driver
            from apps.reservations.models import Reservation
            from apps.trips.models import Trip
            from apps.vehicles.models import Vehicle

            def grouped(qs, **filt):
                base = qs.filter(subsidiary_id__in=ids)
                if filt:
                    base = base.filter(**filt)
                return {str(r["subsidiary"]): r["n"] for r in base.values("subsidiary").annotate(n=Count("id"))}

            veh = grouped(Vehicle.objects)
            veh_avail = grouped(Vehicle.objects, status="available")
            drv = grouped(Driver.objects)
            trips_ip = grouped(Trip.objects, status="in_progress")
            res_pending = grouped(Reservation.objects, status__in=_RES_PENDING)
            for r in rows:
                sid = str(r["id"])
                r["stats"] = {
                    "vehicles": veh.get(sid, 0),
                    "vehicles_available": veh_avail.get(sid, 0),
                    "drivers": drv.get(sid, 0),
                    "trips_in_progress": trips_ip.get(sid, 0),
                    "reservations_pending": res_pending.get(sid, 0),
                }
        return response

    @action(detail=True, methods=["get"])
    def stats(self, request, pk=None):
        """Statistiques détaillées d'une filiale (scopées : 404 hors périmètre)."""
        sub = self.get_object()
        return Response({"subsidiary": SubsidiarySerializer(sub).data, **subsidiary_stats(sub, request.user)})

    def _check_write(self):
        u = self.request.user
        if not (u.is_superuser or u.has_company_scope):
            raise PermissionDenied("Gestion des filiales réservée au périmètre entreprise.")

    def perform_create(self, serializer):
        self._check_write()
        company = serializer.validated_data.get("company") or Company.objects.first()
        serializer.save(company=company, created_by=self.request.user)

    def perform_update(self, serializer):
        self._check_write()
        serializer.save()

    def perform_destroy(self, instance):
        self._check_write()
        instance.delete()
