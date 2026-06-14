"""Statistiques agrégées du dashboard + assistant K-BOT (ancré sur les données)."""
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Sum
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.analytics.scope import scoped
from apps.audit import services as audit
from apps.core.enums import AuditAction, ReservationStatus, VehicleStatus
from apps.kbot.engine import answer_question


def _f(value):
    return float(value) if isinstance(value, Decimal) else (value or 0)


class DashboardStatsView(APIView):
    """Dashboard décisionnel : statistiques d'activité réelle par période.

    Filtres : ?period=week|month|year|custom (&start=&end=), &subsidiary=,
    &status= (statut de réservation), &vehicle=.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.analytics.decision import decision_stats

        return Response(decision_stats(request.user, request.query_params))


class LegacyDashboardStatsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        qs = scoped(user, request.query_params.get("subsidiary"))
        today = timezone.localdate()
        now = timezone.now()

        vehicles = qs["vehicles"]
        total_vehicles = vehicles.count()
        status_counts = {
            row["status"]: row["n"]
            for row in vehicles.values("status").annotate(n=Count("id"))
        }
        sc = lambda s: status_counts.get(s, 0)  # noqa: E731

        reservations = qs["reservations"]
        res_status = {
            row["status"]: row["n"]
            for row in reservations.values("status").annotate(n=Count("id"))
        }
        rc = lambda s: res_status.get(s, 0)  # noqa: E731

        trips = qs["trips"]
        active_trips = trips.filter(status="in_progress")
        late = active_trips.filter(reservation__estimated_return__lt=now).count()
        total_km = _f(trips.aggregate(s=Sum("distance_km"))["s"])
        completed = trips.filter(status__in=["returned", "closed"]).count()

        drivers = qs["drivers"]
        drivers_on_trip = (
            drivers.filter(reservations__status="in_progress").distinct().count()
        )

        fuel_cost = _f(qs["fuel"].aggregate(s=Sum("amount"))["s"])
        maint_cost = _f(qs["maintenance"].aggregate(s=Sum("cost"))["s"])
        other_cost = _f(qs["expenses"].aggregate(s=Sum("amount"))["s"])
        total_cost = fuel_cost + maint_cost + other_cost

        # Séries — courses par jour (14 derniers jours)
        days = []
        for i in range(13, -1, -1):
            d = today - timedelta(days=i)
            days.append({
                "date": d.isoformat(),
                "label": d.strftime("%d/%m"),
                "count": reservations.filter(trip_date=d).count(),
            })

        # Ventilation par filiale (périmètre entreprise)
        by_subsidiary = []
        if user.is_superuser or user.has_company_scope:
            for row in (
                vehicles.values("subsidiary__name")
                .annotate(n=Count("id"))
                .order_by("-n")
            ):
                name = row["subsidiary__name"]
                sub_cost = _f(
                    qs["fuel"].filter(subsidiary__name=name).aggregate(s=Sum("amount"))["s"]
                )
                by_subsidiary.append({
                    "name": name,
                    "vehicles": row["n"],
                    "courses": reservations.filter(subsidiary__name=name).count(),
                    "fuel_cost": sub_cost,
                })

        # Top véhicules les plus utilisés
        top_vehicles = [
            {"registration": r["vehicle__registration"], "trips": r["n"]}
            for r in (
                trips.values("vehicle__registration")
                .annotate(n=Count("id"))
                .order_by("-n")[:5]
            )
        ]

        rate = lambda part: round(part / total_vehicles * 100, 1) if total_vehicles else 0  # noqa: E731

        data = {
            "fleet": {
                "total": total_vehicles,
                "available": sc(VehicleStatus.AVAILABLE),
                "reserved": sc(VehicleStatus.RESERVED),
                "on_trip": sc(VehicleStatus.ON_TRIP),
                "maintenance": sc(VehicleStatus.MAINTENANCE),
                "out_of_service": sc(VehicleStatus.OUT_OF_SERVICE),
                "unavailable": sc(VehicleStatus.UNAVAILABLE),
            },
            "courses": {
                "today": reservations.filter(trip_date=today).count(),
                "pending": rc(ReservationStatus.PENDING_MANAGER) + rc(ReservationStatus.PENDING_FLEET) + rc(ReservationStatus.SUBMITTED),
                "approved": rc(ReservationStatus.APPROVED) + rc(ReservationStatus.VEHICLE_ASSIGNED) + rc(ReservationStatus.DRIVER_ASSIGNED),
                "rejected": rc(ReservationStatus.REJECTED),
                "in_progress": rc(ReservationStatus.IN_PROGRESS),
                "completed": completed,
            },
            "drivers": {
                "total": drivers.count(),
                "available": drivers.filter(is_available=True).count(),
                "on_trip": drivers_on_trip,
            },
            "rates": {
                "utilization": rate(sc(VehicleStatus.ON_TRIP) + sc(VehicleStatus.RESERVED)),
                "immobilization": rate(sc(VehicleStatus.MAINTENANCE) + sc(VehicleStatus.OUT_OF_SERVICE)),
                "late": round(late / active_trips.count() * 100, 1) if active_trips.count() else 0,
                "availability": rate(sc(VehicleStatus.AVAILABLE)),
            },
            "totals": {
                "km": round(total_km, 1),
                "cost_total": round(total_cost, 2),
                "cost_fuel": round(fuel_cost, 2),
                "cost_maintenance": round(maint_cost, 2),
                "cost_per_course": round(total_cost / completed, 2) if completed else 0,
                "cost_per_km": round(total_cost / total_km, 2) if total_km else 0,
            },
            "charts": {
                "courses_per_day": days,
                "vehicles_by_status": [
                    {"status": "available", "label": "Disponible", "count": sc(VehicleStatus.AVAILABLE)},
                    {"status": "reserved", "label": "Réservé", "count": sc(VehicleStatus.RESERVED)},
                    {"status": "on_trip", "label": "En course", "count": sc(VehicleStatus.ON_TRIP)},
                    {"status": "maintenance", "label": "Maintenance", "count": sc(VehicleStatus.MAINTENANCE)},
                    {"status": "out_of_service", "label": "Hors service", "count": sc(VehicleStatus.OUT_OF_SERVICE)},
                ],
                "costs": [
                    {"name": "Carburant", "value": round(fuel_cost, 2)},
                    {"name": "Maintenance", "value": round(maint_cost, 2)},
                    {"name": "Autres", "value": round(other_cost, 2)},
                ],
            },
            "by_subsidiary": by_subsidiary,
            "top_vehicles": top_vehicles,
            "scope": "company" if (user.is_superuser or user.has_company_scope) else "subsidiary",
            "subsidiary_name": user.subsidiary.name if user.subsidiary_id else None,
        }
        return Response(data)


class AlertsView(APIView):
    """Alertes agrégées : expirations (assurance/visite/permis), maintenance, retards."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from datetime import timedelta

        from apps.drivers.models import Driver
        from apps.maintenance.models import MaintenanceSchedule
        from apps.vehicles.models import VehicleDocument

        qs = scoped(request.user)
        today = timezone.localdate()
        now = timezone.now()
        soon = today + timedelta(days=30)
        alerts = []

        def sev(d):
            return "critical" if d and d < today else "warning"

        # Documents véhicule (assurance, visite technique, carte grise)
        for doc in VehicleDocument.objects.filter(
            vehicle__in=qs["vehicles"], expiry_date__isnull=False, expiry_date__lte=soon
        ).select_related("vehicle")[:50]:
            alerts.append({
                "type": "document",
                "severity": sev(doc.expiry_date),
                "title": f"{doc.get_doc_type_display()} — {doc.vehicle.registration}",
                "detail": "Expirée" if doc.expiry_date < today else "Expire bientôt",
                "date": doc.expiry_date.isoformat(),
            })

        # Conformité : assurances, visites techniques, révisions (modèles dédiés)
        from apps.vehicles.compliance import next_revision_km, revision_remaining_km
        from apps.vehicles.models import InsurancePolicy, TechnicalInspection

        for ins in InsurancePolicy.objects.filter(
            vehicle__in=qs["vehicles"], expiry_date__lte=soon
        ).select_related("vehicle")[:50]:
            alerts.append({
                "type": "insurance",
                "severity": sev(ins.expiry_date),
                "title": f"Assurance — {ins.vehicle.registration}",
                "detail": (
                    f"{ins.company} : expirée — véhicule non conforme"
                    if ins.expiry_date < today else f"{ins.company} : expire bientôt"
                ),
                "date": ins.expiry_date.isoformat(),
            })

        for insp in TechnicalInspection.objects.filter(
            vehicle__in=qs["vehicles"], next_date__lte=soon
        ).select_related("vehicle")[:50]:
            alerts.append({
                "type": "inspection",
                "severity": sev(insp.next_date),
                "title": f"Visite technique — {insp.vehicle.registration}",
                "detail": "Expirée — véhicule non conforme" if insp.next_date < today else "À passer bientôt",
                "date": insp.next_date.isoformat(),
            })

        for v in qs["vehicles"].prefetch_related("revisions"):
            if not (v.revisions.exists() or v.mileage >= 10_000):
                continue
            remaining = revision_remaining_km(v)
            if remaining > 2_000:
                continue
            alerts.append({
                "type": "revision",
                "severity": "critical" if remaining <= 0 else "warning",
                "title": f"Révision — {v.registration}",
                "detail": (
                    f"Dépassée de {abs(remaining)} km (seuil {next_revision_km(v)} km) — véhicule non conforme"
                    if remaining <= 0 else f"Dans {remaining} km (seuil {next_revision_km(v)} km)"
                ),
                "date": today.isoformat(),
            })

        # Permis chauffeurs
        for d in qs["drivers"].filter(license_expiry__isnull=False, license_expiry__lte=soon)[:50]:
            alerts.append({
                "type": "license",
                "severity": sev(d.license_expiry),
                "title": f"Permis — {d.full_name}",
                "detail": "Permis expiré" if d.license_expiry < today else "Permis expire bientôt",
                "date": d.license_expiry.isoformat(),
            })

        # Maintenance à échéance
        for s in MaintenanceSchedule.objects.filter(
            vehicle__in=qs["vehicles"], is_active=True, due_date__isnull=False, due_date__lte=soon
        ).select_related("vehicle", "maintenance_type")[:50]:
            alerts.append({
                "type": "maintenance",
                "severity": sev(s.due_date),
                "title": f"{s.maintenance_type.name} — {s.vehicle.registration}",
                "detail": "Maintenance à prévoir",
                "date": s.due_date.isoformat(),
            })

        # Alertes géofence récentes (48 h)
        from apps.tracking.models import GeofenceAlert

        for ga in (
            GeofenceAlert.objects.filter(
                vehicle__in=qs["vehicles"], occurred_at__gte=now - timedelta(hours=48)
            ).select_related("zone", "vehicle").order_by("-occurred_at")[:30]
        ):
            alerts.append({
                "type": "geofence",
                "severity": ga.severity if ga.severity in ("critical", "warning") else "warning",
                "title": f"{'Sortie de' if ga.event == 'exit' else 'Entrée en'} zone — {ga.vehicle.registration}",
                "detail": f"Zone « {ga.zone.name} » ({ga.zone.get_zone_type_display()})",
                "date": ga.occurred_at.date().isoformat(),
            })

        # Courses en retard
        for t in qs["trips"].filter(status="in_progress").select_related("reservation", "vehicle"):
            er = t.reservation.estimated_return
            if er and er < now:
                alerts.append({
                    "type": "late",
                    "severity": "critical",
                    "title": f"Retour en retard — {t.vehicle.registration}",
                    "detail": f"Retour attendu : {er.strftime('%d/%m %H:%M')}",
                    "date": er.date().isoformat(),
                })

        order = {"critical": 0, "warning": 1, "info": 2}
        alerts.sort(key=lambda a: (order.get(a["severity"], 3), a["date"]))
        counts = {
            "critical": sum(1 for a in alerts if a["severity"] == "critical"),
            "warning": sum(1 for a in alerts if a["severity"] == "warning"),
            "total": len(alerts),
        }
        return Response({"counts": counts, "results": alerts})


class KBotView(APIView):
    """Assistant K-BOT : réponses ancrées sur les données autorisées de l'utilisateur."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        question = (request.data.get("question") or "").strip()
        if not question:
            return Response({"answer": "Posez-moi une question sur votre flotte.", "data": None})
        # Position éventuelle du demandeur, pour les intentions « au plus proche » (#3E).
        origin = None
        try:
            lat, lng = request.data.get("lat"), request.data.get("lng")
            if lat is not None and lng is not None:
                origin = (float(lat), float(lng))
        except (TypeError, ValueError):
            origin = None
        result = answer_question(request.user, question, origin=origin)
        audit.record(
            request.user, AuditAction.ACCESS, None,
            changes={"kbot_question": question[:255], "intent": result.get("intent")},
            request=request,
        )
        return Response(result)
