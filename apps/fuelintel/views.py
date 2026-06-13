"""Fleet Fuel Intelligence — tableau de bord carburant (gestionnaires & admins)."""
from datetime import timedelta
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.fuelintel.access import can_see_costs
from apps.fuelintel.engine import FUEL_CODE_BY_TYPE
from apps.fuelintel.models import FuelConsumptionProfile, FuelPrice


def _f(value):
    return float(value) if value is not None else 0.0


class FuelIntelView(APIView):
    """Indicateurs Fuel Intelligence : consommation, coûts, tops, écarts, prix."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not can_see_costs(request.user):
            raise PermissionDenied("Réservé aux gestionnaires de flotte et administrateurs.")

        from apps.expenses.models import FuelLog
        from apps.trips.models import Trip

        today = timezone.localdate()
        month_start = today.replace(day=1)

        logs = FuelLog.objects.all()
        day_l = _f(logs.filter(date=today).aggregate(s=Sum("liters"))["s"])
        day_cost = _f(logs.filter(date=today).aggregate(s=Sum("amount"))["s"])
        month_l = _f(logs.filter(date__gte=month_start).aggregate(s=Sum("liters"))["s"])
        month_cost = _f(logs.filter(date__gte=month_start).aggregate(s=Sum("amount"))["s"])

        # Prévision mensuelle : extrapolation de la moyenne quotidienne observée.
        elapsed = max(1, (today - month_start).days + 1)
        days_in_month = 30
        forecast_l = round(month_l / elapsed * days_in_month, 1)
        forecast_cost = round(month_cost / elapsed * days_in_month)

        # Profils appris : tops + alertes de surconsommation
        fleet = FuelConsumptionProfile.objects.filter(scope="fleet").first()
        fleet_rate = float(fleet.rate_l_per_100km) if fleet else None

        vehicles = list(
            FuelConsumptionProfile.objects.filter(scope="vehicle", samples__gte=1)
            .order_by("-rate_l_per_100km")[:5]
            .values("label", "rate_l_per_100km", "samples")
        )
        drivers = list(
            FuelConsumptionProfile.objects.filter(scope="driver", samples__gte=1)
            .order_by("rate_l_per_100km")[:5]
            .values("label", "rate_l_per_100km", "samples")
        )
        subsidiaries = list(
            FuelConsumptionProfile.objects.filter(scope="subsidiary", samples__gte=1)
            .order_by("rate_l_per_100km")
            .values("label", "rate_l_per_100km", "samples")
        )
        overconsumption = []
        if fleet_rate:
            for v in vehicles:
                rate = float(v["rate_l_per_100km"])
                if rate > fleet_rate * 1.25 and v["samples"] >= 3:
                    overconsumption.append({
                        "label": v["label"],
                        "rate": rate,
                        "fleet_rate": fleet_rate,
                        "excess_pct": round((rate / fleet_rate - 1) * 100),
                    })

        # Écart prévision / réel sur les courses clôturées du mois
        gap_rows = Trip.objects.filter(
            actual_return__date__gte=month_start,
            fuel_consumed__isnull=False, route__estimated_fuel_l__isnull=False,
        ).values_list("fuel_consumed", "route__estimated_fuel_l")
        est_sum = sum((Decimal(e) for _, e in gap_rows), Decimal("0"))
        real_sum = sum((Decimal(r) for r, _ in gap_rows), Decimal("0"))
        gap_pct = round(float((real_sum - est_sum) / est_sum * 100), 1) if est_sum else None

        # Prix carburant + historique des variations
        prices = {}
        for code, label in FuelPrice.FUEL_CHOICES:
            latest = FuelPrice.latest(code)
            history = list(
                FuelPrice.objects.filter(fuel_code=code)
                .order_by("-effective_date")[:6]
                .values("price", "effective_date")
            )
            prices[code] = {
                "label": label,
                "price": _f(latest.price) if latest else None,
                "date": latest.effective_date.isoformat() if latest else None,
                "history": [{"price": _f(h["price"]), "date": h["effective_date"].isoformat()} for h in history],
            }

        return Response({
            "day": {"liters": day_l, "cost": day_cost},
            "month": {"liters": month_l, "cost": month_cost},
            "forecast": {"liters": forecast_l, "cost": forecast_cost},
            "fleet_rate": fleet_rate,
            "gap_pct": gap_pct,
            "top_vehicles": [
                {"label": v["label"], "rate": float(v["rate_l_per_100km"]), "samples": v["samples"]}
                for v in vehicles
            ],
            "top_drivers": [
                {"label": d["label"], "rate": float(d["rate_l_per_100km"]), "samples": d["samples"]}
                for d in drivers
            ],
            "subsidiaries": [
                {"label": s["label"], "rate": float(s["rate_l_per_100km"]), "samples": s["samples"]}
                for s in subsidiaries
            ],
            "overconsumption": overconsumption,
            "prices": prices,
            "fuel_code_map": FUEL_CODE_BY_TYPE,
        })
