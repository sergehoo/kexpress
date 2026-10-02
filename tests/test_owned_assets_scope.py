"""Coûts, conformité, occupation et alertes : sur les véhicules POSSÉDÉS, pas sur la flotte.

`scoped()["vehicles"]` couvre toute la flotte mutualisée — c'est voulu pour la LECTURE
(disponibilités, carte, réservation d'un véhicule d'une sœur). Mais plusieurs calculs de
gestion s'appuyaient dessus : un gestionnaire d'Abidjan voyait dans SON tableau de bord les
primes d'assurance de Dakar, les alertes d'échéance de toute la flotte, l'occupation des
véhicules des autres filiales et les classements nominatifs de leurs chauffeurs. Double
défaut : fuite de données, et chiffres faux pour le lecteur.

Ce que la gestion d'un actif regarde, c'est le PROPRIÉTAIRE : `scoped()["owned_vehicles"]`.

Cas particulier de l'occupation : l'usage d'un véhicule possédé est mesuré TOUTES filiales
confondues (un véhicule prêté 80 % du temps n'est pas inutilisé), en agrégats seulement —
et désormais de façon identique que la journée vienne du cache nocturne ou du calcul direct.
"""
from datetime import datetime, time, timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.enums import ReservationStatus, RoleChoices, TripStatus, TripType
from apps.vehicles.models import InsurancePolicy, Vehicle

pytestmark = pytest.mark.django_db


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def own_vehicle(sub_a):
    return Vehicle.objects.create(subsidiary=sub_a, registration="OWN-A1", brand="T",
                                  model="X", capacity=5)


@pytest.fixture
def sister_vehicle(sub_b):
    return Vehicle.objects.create(subsidiary=sub_b, registration="SIS-B1", brand="R",
                                  model="Y", capacity=5)


@pytest.fixture
def requester_b(sub_b):
    return User.objects.create_user("oas-req-b@test.io", "pw", role=RoleChoices.REQUESTER,
                                    subsidiary=sub_b)


def _insure(vehicle, *, cost, company, days_left=10):
    today = timezone.localdate()
    return InsurancePolicy.objects.create(
        vehicle=vehicle, company=company, policy_number=f"P-{vehicle.registration}",
        start_date=today, expiry_date=today + timedelta(days=days_left), cost=Decimal(cost),
    )


def _drive(vehicle, sub, requester, *, day, passengers=2, km=20, hour=8):
    from apps.reservations.models import Reservation
    from apps.reservations.services import _ensure_trips
    from apps.trips.models import Trip

    dep = timezone.make_aware(datetime.combine(day, time(hour, 0)),
                              timezone.get_current_timezone())
    res = Reservation.objects.create(
        subsidiary=sub, requester=requester, created_by=requester, trip_date=day,
        departure_time=dep, estimated_return=dep + timedelta(hours=1), origin="Cocody",
        destination=f"Plateau {vehicle.registration} {hour}", purpose="Mission",
        passengers=passengers, needs_driver=False, trip_type=TripType.ONE_WAY,
        status=ReservationStatus.APPROVED,
    )
    trip = _ensure_trips(res)[0]
    Trip.objects.filter(pk=trip.pk).update(
        vehicle=vehicle, actual_departure=dep, actual_return=dep + timedelta(hours=1),
        distance_km=Decimal(km), status=TripStatus.CLOSED,
    )
    return trip


# --- Tableau de bord décisionnel --------------------------------------------


def test_dashboard_costs_exclude_sister_owned_vehicles(
    api, fleet_a, company_admin, own_vehicle, sister_vehicle
):
    _insure(own_vehicle, cost="100000", company="NSIA")
    _insure(sister_vehicle, cost="777000", company="SUNU")

    api.force_authenticate(fleet_a)
    body = api.get(reverse("dashboard-stats"), {"period": "month"}).json()
    insurance = next(row for row in body["cost"]["detail"] if row["key"] == "insurance")
    assert insurance["value"] == 100000, "la prime d'une filiale sœur est comptée"
    assert body["compliance"]["annual_insurance_cost"] == 100000
    assert body["compliance"]["vehicles_total"] == 1

    api.force_authenticate(company_admin)
    body = api.get(reverse("dashboard-stats"), {"period": "month"}).json()
    insurance = next(row for row in body["cost"]["detail"] if row["key"] == "insurance")
    assert insurance["value"] == 877000, "le périmètre groupe consolide toutes les filiales"


# --- Occupation ----------------------------------------------------------------


def test_occupancy_lists_only_owned_vehicles(api, fleet_a, own_vehicle, sister_vehicle):
    api.force_authenticate(fleet_a)
    body = api.get(reverse("dashboard-occupancy"), {"period": "month"}).json()
    registrations = {row["registration"] for row in body["results"]}
    assert "SIS-B1" not in registrations
    assert "OWN-A1" in registrations


def test_occupancy_is_reserved_to_managers(api, requester_a):
    """Le frontend ne l'affiche qu'aux gestionnaires : l'API s'aligne."""
    api.force_authenticate(requester_a)
    assert api.get(reverse("dashboard-occupancy")).status_code == 403


def test_occupancy_is_identical_from_cache_and_from_live_computation(
    fleet_a, sub_a, sub_b, requester_a, requester_b, own_vehicle
):
    """Le cache nocturne mesure l'usage de TOUTES les courses du véhicule ; le calcul direct
    ne voyait que celles de la filiale du lecteur. Le même jour affichait donc deux
    chiffres selon que la tâche nocturne avait tourné ou non."""
    from apps.analytics.metrics import fleet_occupancy
    from apps.analytics.tasks import recompute_metrics

    day = timezone.localdate() - timedelta(days=1)
    _drive(own_vehicle, sub_a, requester_a, day=day, passengers=2, hour=8)
    _drive(own_vehicle, sub_b, requester_b, day=day, passengers=3, hour=11)  # prêt à Dakar
    window = {"period": "custom", "start": day.isoformat(), "end": day.isoformat()}

    def own_row():
        rows = fleet_occupancy(fleet_a, window)["results"]
        return next(r for r in rows if r["registration"] == "OWN-A1")

    live = own_row()
    recompute_metrics(day=day.isoformat())
    cached = own_row()

    assert live["trips"] == cached["trips"] == 2
    assert live["passengers_carried"] == cached["passengers_carried"] == 5


# --- Prévision de maintenance ------------------------------------------------


def test_maintenance_forecast_covers_owned_vehicles_only(
    api, fleet_a, own_vehicle, sister_vehicle
):
    api.force_authenticate(fleet_a)
    rows = api.get(reverse("maintenance-forecast")).json()["results"]
    registrations = {row["registration"] for row in rows}
    assert "SIS-B1" not in registrations and "OWN-A1" in registrations


# --- Alertes ---------------------------------------------------------------------


def _alert_titles(api, user):
    api.force_authenticate(user)
    return [a["title"] + " " + (a.get("detail") or "")
            for a in api.get(reverse("alerts")).json()["results"]]


def test_compliance_alerts_cover_owned_vehicles_only(
    api, fleet_a, own_vehicle, sister_vehicle
):
    _insure(own_vehicle, cost="1", company="NSIA")
    _insure(sister_vehicle, cost="1", company="SUNU-SECRET")
    titles = " | ".join(_alert_titles(api, fleet_a))
    assert "OWN-A1" in titles
    assert "SIS-B1" not in titles and "SUNU-SECRET" not in titles


def test_licence_alerts_cover_own_drivers_only(api, fleet_a, sub_a, sub_b):
    from apps.drivers.models import Driver

    soon = timezone.localdate() + timedelta(days=5)
    Driver.objects.create(subsidiary=sub_a, first_name="Koffi", last_name="Propre",
                          license_expiry=soon)
    Driver.objects.create(subsidiary=sub_b, first_name="Awa", last_name="Soeur",
                          license_expiry=soon)
    titles = " | ".join(_alert_titles(api, fleet_a))
    assert "Propre" in titles and "Soeur" not in titles


def test_sister_grouping_suggestions_stay_out_of_the_alert_feed(api, fleet_a, sub_b):
    """Le rationale d'une suggestion cite destinations et passagers d'une autre filiale."""
    from apps.dispatch.models import DispatchSuggestion

    DispatchSuggestion.objects.create(
        kind="group", status="proposed", generated_for=sub_b, score=0.9,
        rationale="Regrouper Plateau-SECRET-DAKAR (3 passagers)",
    )
    titles = " | ".join(_alert_titles(api, fleet_a))
    assert "SECRET-DAKAR" not in titles


def test_requesters_get_no_fleet_management_alerts(api, requester_a, own_vehicle):
    """Un demandeur ne gère ni la conformité ni la maintenance de la flotte."""
    _insure(own_vehicle, cost="1", company="NSIA")
    titles = " | ".join(_alert_titles(api, requester_a))
    assert "OWN-A1" not in titles


# --- Classements de consommation (tableau énergie + K-BOT) -------------------


@pytest.fixture
def profiles(sub_a, sub_b):
    from apps.drivers.models import Driver
    from apps.fuelintel.models import FuelConsumptionProfile

    own = Driver.objects.create(subsidiary=sub_a, first_name="Koffi", last_name="Propre")
    sister = Driver.objects.create(subsidiary=sub_b, first_name="Awa", last_name="Soeur")
    for driver, rate in ((own, "7.5"), (sister, "6.1")):
        FuelConsumptionProfile.objects.create(
            scope="driver", ref=str(driver.id), label=driver.full_name, unit="L",
            rate_l_per_100km=Decimal(rate), samples=5,
        )
    return own, sister


def test_energy_dashboard_ranks_only_own_drivers(api, fleet_a, profiles):
    api.force_authenticate(fleet_a)
    body = api.get(reverse("fuel-intel")).json()
    labels = {row["label"] for row in body["top_drivers"]}
    assert "Koffi Propre" in labels and "Awa Soeur" not in labels


def test_kbot_ranks_only_own_drivers(fleet_a, profiles):
    from apps.kbot.engine import _fuel_drivers

    text = str(_fuel_drivers(fleet_a))
    assert "Koffi Propre" in text and "Awa Soeur" not in text


def test_company_scope_keeps_the_group_wide_ranking(api, company_admin, profiles):
    api.force_authenticate(company_admin)
    labels = {row["label"] for row in api.get(reverse("fuel-intel")).json()["top_drivers"]}
    assert {"Koffi Propre", "Awa Soeur"} <= labels
