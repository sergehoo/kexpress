"""Tâches périodiques Fuel Intelligence : recalibrage du modèle + prix carburant CI."""
from __future__ import annotations

import json
import logging
import urllib.request
from collections import defaultdict
from decimal import Decimal

from celery import shared_task
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

#: Pseudo-kilométrage d'a priori (shrinkage) : l'a priori constructeur pèse comme
#: 200 km d'observations — vite dominé par les données réelles de la flotte.
PRIOR_KM = Decimal("200")


@shared_task
def recalibrate_fuel_model() -> dict:
    """Recalcule tous les profils de consommation depuis les courses réelles.

    Source : courses clôturées avec distance ET carburant consommé renseignés.
    Lissage bayésien simple : taux = (Σ litres + a_priori×PRIOR_KM/100)
                                     / (Σ km + PRIOR_KM) × 100.
    """
    from apps.fuelintel.engine import BASE_RATE_BY_FUEL
    from apps.fuelintel.models import FuelConsumptionProfile
    from apps.trips.models import Trip

    trips = Trip.objects.filter(
        distance_km__isnull=False, fuel_consumed__isnull=False, distance_km__gt=0,
    ).select_related("vehicle", "driver", "subsidiary")

    # Agrégats par niveau : {(scope, ref): [km, litres, n, label, prior]}
    agg: dict[tuple, list] = defaultdict(lambda: [Decimal("0"), Decimal("0"), 0, "", None])

    def feed(scope, ref, label, km, liters, prior):
        row = agg[(scope, str(ref))]
        row[0] += km; row[1] += liters; row[2] += 1
        row[3] = label; row[4] = prior

    for t in trips:
        km = Decimal(t.distance_km)
        liters = Decimal(t.fuel_consumed)
        prior = BASE_RATE_BY_FUEL.get(t.vehicle.fuel_type, BASE_RATE_BY_FUEL["other"])
        feed("vehicle", t.vehicle_id, t.vehicle.registration, km, liters, prior)
        if t.driver_id:
            feed("driver", t.driver_id, t.driver.full_name, km, liters, prior)
        feed("vehicle_type", t.vehicle.vehicle_type, t.vehicle.get_vehicle_type_display(), km, liters, prior)
        feed("subsidiary", t.subsidiary_id, t.subsidiary.name, km, liters, prior)
        feed("fleet", "", "Flotte", km, liters, prior)

    updated = 0
    for (scope, ref), (km, liters, n, label, prior) in agg.items():
        prior = prior or Decimal("8.0")
        rate = ((liters + prior * PRIOR_KM / 100) / (km + PRIOR_KM) * 100).quantize(Decimal("0.01"))
        FuelConsumptionProfile.objects.update_or_create(
            scope=scope, ref=str(ref),
            defaults={"label": label[:255], "rate_l_per_100km": rate,
                      "samples": n, "total_km": km, "total_liters": liters},
        )
        updated += 1
    logger.info("Fuel model recalibré : %s profils.", updated)
    return {"profiles": updated, "trips": trips.count()}


@shared_task
def update_fuel_prices() -> dict:
    """Met à jour les prix carburant (Côte d'Ivoire).

    Source primaire : URL JSON configurable `FUEL_PRICE_SOURCE_URL`
    (format attendu : {"super": 875, "gasoil": 655, "date": "2026-06-01"}).
    Repli : valeurs configurées `FUEL_PRICE_SUPER` / `FUEL_PRICE_GASOIL`
    (réglementées, publiées par arrêté interministériel CI).
    Une ligne n'est créée que si le prix change → historique des variations.
    """
    from apps.fuelintel.models import FuelPrice

    source_url = getattr(settings, "FUEL_PRICE_SOURCE_URL", "")
    prices: dict[str, Decimal] = {}
    effective = timezone.localdate()
    source = "configuration"

    if source_url:
        try:
            req = urllib.request.Request(source_url, headers={"User-Agent": "KaydanExpress/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.load(resp)
            prices = {"super": Decimal(str(data["super"])), "gasoil": Decimal(str(data["gasoil"]))}
            if data.get("date"):
                from datetime import date
                effective = date.fromisoformat(data["date"])
            source = source_url[:255]
        except Exception as exc:
            logger.warning("Source prix carburant injoignable (%s) — repli configuration.", exc)

    if not prices:
        prices = {
            "super": Decimal(str(getattr(settings, "FUEL_PRICE_SUPER", "875"))),
            "gasoil": Decimal(str(getattr(settings, "FUEL_PRICE_GASOIL", "655"))),
        }

    created = 0
    for code, price in prices.items():
        last = FuelPrice.latest(code)
        if last is None or last.price != price:
            FuelPrice.objects.create(
                fuel_code=code, price=price, effective_date=effective, source=source,
            )
            created += 1
            _notify_price_change(code, last.price if last else None, price, effective)
    return {"created": created, "prices": {k: str(v) for k, v in prices.items()}}


def _notify_price_change(code, old_price, new_price, effective):
    """Prix carburant local mis à jour → finance + administrateurs entreprise."""
    from apps.accounts.models import User
    from apps.core.enums import NotificationType, RoleChoices
    from apps.notifications.services import notify_many

    label = "Gasoil" if code == "gasoil" else "Super sans plomb"
    variation = (
        f" (précédent : {old_price} XOF/L)" if old_price is not None else ""
    )
    recipients = list(User.objects.filter(is_active=True, role__in=[
        RoleChoices.FINANCE, RoleChoices.COMPANY_ADMIN, RoleChoices.FLEET_MANAGER,
    ]))
    notify_many(
        recipients, NotificationType.FUEL_PRICE_UPDATED,
        title=f"Prix carburant mis à jour — {label}",
        message=f"{label} : {new_price} XOF/L au {effective:%d/%m/%Y}{variation}.",
        link="/fleet-control",
    )
