"""§8-9 — Aucune donnée financière interne ne parvient à un demandeur, un chauffeur ou un
chef de service, quelle que soit la route d'API qu'il appelle.

Méthode : on sème la base de montants SENTINELLES (valeurs uniques, impossibles à obtenir
par hasard) sur tout ce qui porte un coût — barème, coût de sa propre course, pleins,
recharges, dépenses, maintenance, assurance, visite, révision, valeur d'achat — puis on
appelle, avec chacun de ces profils :
- TOUTES les routes GET sans paramètre de l'API, découvertes automatiquement (une route
  ajoutée demain est couverte sans modifier ce test) ;
- les fiches détaillées de SES propres objets (sa course, sa réservation, le véhicule) ;
- K-BOT, sur les questions de coûts.
Aucune réponse ne doit contenir une sentinelle. Le contrôle positif vérifie qu'un
gestionnaire, lui, les reçoit bien — sans quoi le test ne prouverait rien.
"""
import re
from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import URLPattern, URLResolver, get_resolver, reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.enums import ReservationStatus, RoleChoices, TripType

pytestmark = pytest.mark.django_db

# Sept chiffres au moins, cherchés sans voisin hexadécimal : impossibles dans un UUID (groupes
# de 4, 8 ou 12 caractères hexadécimaux) comme dans un horodatage (6 chiffres de microsecondes
# au plus). Avec six chiffres, un identifiant tiré au hasard a déjà fait échouer ce test.
SENTINELS = {
    "tarif/km": "333.33",
    "coût estimé de la course": "999.99",
    "plein": "9876543",
    "recharge": "9765431",
    "dépense": "8765431",
    "maintenance": "7654321",
    "assurance": "6543219",
    "visite technique": "6432197",
    "révision": "6321987",
    "valeur d'achat": "54321987",
    "charge véhicule": "5432198",
    "prix d'acquisition": "43219876",
}

COST_QUESTIONS = [
    "Quels sont les coûts du mois ?",
    "Quelle est la consommation électrique des véhicules ce mois-ci ?",
    "Compare les coûts énergétiques des véhicules thermiques et électriques",
    "Combien a coûté ma course ?",
    "Quels véhicules consomment trop ?",
    "Résumé de la journée",
]

SKIPPED_PREFIXES = ("admin/", "api/schema", "api/docs", "api/redoc", "media/", "static/")


def _static_get_routes() -> list[str]:
    """Toutes les routes sans paramètre d'URL, telles que Django les résout."""
    routes = []

    def walk(patterns, prefix):
        for pattern in patterns:
            route = prefix + str(pattern.pattern)
            if isinstance(pattern, URLResolver):
                walk(pattern.url_patterns, route)
            elif isinstance(pattern, URLPattern):
                routes.append(route)

    walk(get_resolver().url_patterns, "")
    clean = []
    for route in routes:
        if "<" in route or "(?P" in route:
            continue  # route paramétrée : couverte par les fiches détaillées ci-dessous
        path = "/" + re.sub(r"[\^\$]", "", route).replace("\\", "")
        if path.startswith("/api/") and not path.lstrip("/").startswith(SKIPPED_PREFIXES):
            clean.append(path)
    return sorted(set(clean))


@pytest.fixture
def world(sub_a, requester_a, fleet_a):
    from apps.drivers.models import Driver
    from apps.expenses.models import ElectricCharge, Expense, FuelLog
    from apps.finance.models import TripPricingRule
    from apps.finance.trip_pricing import refresh_estimate
    from apps.maintenance.models import MaintenanceRecord, MaintenanceType
    from apps.reservations.models import Reservation
    from apps.reservations.services import _ensure_trips
    from apps.tracking.models import TripRoute
    from apps.trips.models import Trip
    from apps.vehicles.models import InsurancePolicy, TechnicalInspection, Vehicle, VehicleRevision

    today = timezone.localdate()
    TripPricingRule.objects.create(name="Sentinelle", amount_per_km=Decimal("333.33"),
                                   valid_from=today - timedelta(days=30), reason="test")
    vehicle = Vehicle.objects.create(subsidiary=sub_a, registration="FC-A1", brand="T",
                                     model="X", purchase_value=Decimal("54321987"))
    driver_user = User.objects.create_user("fc-driver@test.io", "pw", role=RoleChoices.DRIVER,
                                           subsidiary=sub_a)
    # Un signal crée la fiche chauffeur avec le compte : on la réutilise.
    driver = Driver.objects.filter(user=driver_user).first() or Driver.objects.create(
        subsidiary=sub_a, user=driver_user, first_name="Koffi", last_name="Chauffeur",
    )

    dep = timezone.now() + timedelta(hours=3)
    reservation = Reservation.objects.create(
        subsidiary=sub_a, requester=requester_a, created_by=requester_a, trip_date=dep.date(),
        departure_time=dep, estimated_return=dep + timedelta(hours=2), origin="Cocody",
        destination="Plateau", purpose="Mission", passengers=2, needs_driver=True,
        trip_type=TripType.ONE_WAY, status=ReservationStatus.APPROVED,
    )
    trip = _ensure_trips(reservation)[0]
    Trip.objects.filter(pk=trip.pk).update(vehicle=vehicle, driver=driver)
    TripRoute.objects.create(trip=trip, origin_label="Cocody", destination_label="Plateau",
                             planned_distance_km=Decimal("3"))
    trip.refresh_from_db()
    refresh_estimate(trip)

    FuelLog.objects.create(vehicle=vehicle, subsidiary=sub_a, date=today, liters=Decimal("40"),
                           amount=Decimal("9876543"), price_per_liter=Decimal("800"))
    ElectricCharge.objects.create(vehicle=vehicle, subsidiary=sub_a, date=today,
                                  kwh_recharged=Decimal("40"), amount=Decimal("9765431"))
    Expense.objects.create(vehicle=vehicle, subsidiary=sub_a, date=today, category="toll",
                           label="Péage", amount=Decimal("8765431"))
    MaintenanceRecord.objects.create(
        vehicle=vehicle, subsidiary=sub_a, nature="corrective", status="completed",
        maintenance_type=MaintenanceType.objects.create(name="Vidange FC"),
        performed_date=today, labor_cost=Decimal("7654321"), parts_cost=Decimal("0"),
        cost=Decimal("7654321"),
    )
    InsurancePolicy.objects.create(vehicle=vehicle, company="NSIA", policy_number="P-FC",
                                   start_date=today, expiry_date=today + timedelta(days=10),
                                   cost=Decimal("6543219"))
    TechnicalInspection.objects.create(vehicle=vehicle, last_date=today,
                                       next_date=today + timedelta(days=10), cost=Decimal("6432197"))
    VehicleRevision.objects.create(vehicle=vehicle, date=today, mileage_at_revision=10000,
                                   cost=Decimal("6321987"))
    from apps.finance.models import VehicleAcquisition, VehicleCharge

    VehicleCharge.objects.create(vehicle=vehicle, kind="tax", label="Vignette", amount=Decimal("5432198"),
                                 period_start=today, period_end=today + timedelta(days=30))
    VehicleAcquisition.objects.create(vehicle=vehicle, mode="purchase", acquisition_date=today,
                                      purchase_price=Decimal("43219876"), depreciation_months=60)
    return {"trip": trip, "reservation": reservation, "vehicle": vehicle, "driver_user": driver_user}


def _profiles(world, requester_a, sub_a):
    department_manager = User.objects.create_user("fc-dm@test.io", "pw",
                                                  role=RoleChoices.DEPARTMENT_MANAGER,
                                                  subsidiary=sub_a)
    return {"demandeur": requester_a, "chauffeur": world["driver_user"],
            "chef de service": department_manager}


def _detail_urls(world):
    trip, reservation, vehicle = world["trip"], world["reservation"], world["vehicle"]
    return [
        reverse("trip-detail", args=[trip.pk]),
        reverse("reservation-detail", args=[reservation.pk]),
        reverse("vehicle-detail", args=[vehicle.pk]),
        reverse("trip-route", args=[trip.pk]),
        reverse("trip-pricing", args=[trip.pk]),
        f"/api/vehicle-insurances/?vehicle={vehicle.pk}",
        f"/api/maintenance/?vehicle={vehicle.pk}",
        "/api/reports/export/?type=expenses&fmt=csv",
        "/api/reports/export/?type=maintenance&fmt=csv",
        "/api/finance/trip-costs/?period=month",
        "/api/dashboard/stats/?period=month",
        # F1 — coût réel.
        reverse("trip-cost-sheet", args=[trip.pk]),
        f"/api/finance/vehicles/{vehicle.pk}/costs/?period={timezone.localdate():%Y-%m}",
        f"/api/finance/vehicle-costs/?period={timezone.localdate():%Y-%m}",
        f"/api/finance/trip-cost-sheets/?period={timezone.localdate():%Y-%m}",
        f"/api/finance/subsidiary-costs/?period={timezone.localdate():%Y-%m}",
        f"/api/finance/vehicle-charges/?vehicle={vehicle.pk}",
        f"/api/finance/vehicle-acquisitions/?vehicle={vehicle.pk}",
    ]


def _leaks(body: str) -> list[str]:
    return [label for label, value in SENTINELS.items()
            if re.search(rf"(?<![0-9a-f]){re.escape(value)}(?![0-9a-f])", body)]


def test_the_route_sweep_actually_covers_the_api():
    """Garde-fou du balayage lui-même : s'il ne trouvait plus les routes, il passerait à vide."""
    routes = _static_get_routes()
    for expected in ("/api/expenses/", "/api/fuel/", "/api/trips/", "/api/dashboard/stats/",
                     "/api/finance/trip-pricing-rules/", "/api/alerts/", "/api/dispatch/board/"):
        assert expected in routes, f"{expected} n'est plus découverte par le balayage"


@pytest.mark.parametrize("profile", ["demandeur", "chauffeur", "chef de service"])
def test_no_financial_amount_reaches_non_finance_profiles(world, requester_a, sub_a, profile):
    user = _profiles(world, requester_a, sub_a)[profile]
    api = APIClient()
    api.force_authenticate(user)

    offenders = []
    for url in _static_get_routes() + _detail_urls(world):
        response = api.get(url)
        if response.status_code == 200:
            found = _leaks(response.content.decode("utf-8", errors="ignore"))
            if found:
                offenders.append(f"{url} → {', '.join(found)}")
    for question in COST_QUESTIONS:
        response = api.post(reverse("kbot-ask"), {"question": question}, format="json")
        found = _leaks(response.content.decode("utf-8", errors="ignore"))
        if found:
            offenders.append(f"K-BOT « {question} » → {', '.join(found)}")

    assert not offenders, f"Montants servis au profil « {profile} » :\n" + "\n".join(offenders)


def test_positive_control_a_fleet_manager_does_receive_the_amounts(world, fleet_a):
    """Sans ce contrôle, un balayage qui ne verrait jamais aucun montant passerait à vide."""
    api = APIClient()
    api.force_authenticate(fleet_a)
    bodies = " ".join(
        api.get(url).content.decode("utf-8", errors="ignore") for url in _detail_urls(world)
    ) + " ".join(
        api.get(url).content.decode("utf-8", errors="ignore")
        for url in ("/api/expenses/", "/api/fuel/", "/api/electric-charges/",
                    "/api/finance/trip-pricing-rules/")
    )
    missing = [label for label in SENTINELS if label not in _leaks(bodies)]
    assert not missing, f"le gestionnaire devrait voir : {missing}"
