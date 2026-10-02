"""Correctifs issus de la revue adversariale du barème kilométrique (29/09/2026).

Chaque test reproduit le scénario exact rapporté par la revue.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group, Permission
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.enums import RoleChoices
from apps.vehicles.models import InsurancePolicy, Vehicle

pytestmark = pytest.mark.django_db


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def auditor(db):
    return User.objects.create_user("rv-auditor@test.io", "pw", role=RoleChoices.AUDITOR)


def _grant(user, codename):
    group, _ = Group.objects.get_or_create(name=f"exception-{codename}")
    group.permissions.add(Permission.objects.get(content_type__app_label="finance", codename=codename))
    user.groups.add(group)
    return User.objects.get(pk=user.pk)  # purge le cache de permissions


# --- Permissions ---------------------------------------------------------------


def test_auditor_reads_expenses_but_cannot_write_them(api, auditor, sub_a, vehicle_a):
    """L'auditeur contrôle : il voit tout, il ne saisit ni ne corrige rien."""
    api.force_authenticate(auditor)
    assert api.get(reverse("fuel-list")).status_code == 200
    response = api.post(reverse("fuel-list"), {
        "vehicle": str(vehicle_a.pk), "subsidiary": str(sub_a.pk),
        "date": timezone.localdate().isoformat(), "liters": "10", "amount": "8000",
    }, format="json")
    assert response.status_code == 403


def test_no_group_can_give_a_requester_any_financial_permission(api, requester_a):
    """Règle absolue : même placé par erreur dans un groupe portant un droit financier, un
    demandeur ne reçoit aucun montant."""
    requester = _grant(requester_a, "view_expenses")
    assert requester.has_perm("finance.view_expenses") is False
    api.force_authenticate(requester)
    assert api.get(reverse("fuel-list")).status_code == 403


def test_a_group_exception_does_open_the_financial_export(api, sub_a):
    """Le mécanisme d'exception documenté doit fonctionner, pas seulement le rôle."""
    controller = User.objects.create_user("rv-ctrl@test.io", "pw",
                                          role=RoleChoices.DEPARTMENT_MANAGER, subsidiary=sub_a)
    url = reverse("report-export")
    api.force_authenticate(controller)
    assert api.get(url, {"type": "maintenance", "fmt": "csv"}).status_code == 403
    api.force_authenticate(_grant(controller, "export_financial_reports"))
    assert api.get(url, {"type": "maintenance", "fmt": "csv"}).status_code == 200
    # Le rapport des dépenses suit, en plus, le droit d'export des dépenses (F2, revue C7) :
    # une seule règle pour les deux routes d'export — accordée elle aussi par exception.
    assert api.get(url, {"type": "expenses", "fmt": "csv"}).status_code == 403
    api.force_authenticate(_grant(controller, "export_expenses"))
    assert api.get(url, {"type": "expenses", "fmt": "csv"}).status_code == 200


def test_subsidiary_admin_cannot_create_a_group_finance_account(api, admin_a, sub_a):
    """Escalade confirmée : retirer la filiale d'un financier en faisait un financier groupe
    (barème global, notifications de toutes les filiales)."""
    target = User.objects.create_user("rv-fin@test.io", "pw", role=RoleChoices.REQUESTER,
                                      subsidiary=sub_a)
    api.force_authenticate(admin_a)
    url = reverse("employee-detail", args=[target.pk])
    response = api.patch(url, {"role": "finance", "subsidiary": None}, format="json")
    assert response.status_code == 403
    target.refresh_from_db()
    assert target.subsidiary_id == sub_a.pk and target.role == RoleChoices.REQUESTER
    # Revue F3 + P0 : même DANS sa filiale, l'admin de filiale n'attribue pas un rôle Finance —
    # il conférerait validation et paiement, qu'il n'a pas ; le super administrateur le peut.
    assert api.patch(url, {"role": "finance"}, format="json").status_code == 403
    root = User.objects.create_user("rv-root@test.io", "Racine-Solide-91", role=RoleChoices.SUPER_ADMIN)
    api.force_authenticate(root)
    assert api.patch(url, {"role": "finance"}, format="json").status_code == 200
    target.refresh_from_db()
    assert target.role == RoleChoices.FINANCE and target.subsidiary_id == sub_a.pk


# --- Montants patrimoniaux -------------------------------------------------------


def test_a_profile_without_cost_rights_cannot_write_a_purchase_value(api, sub_a, vehicle_a):
    """Écrire un chiffre qu'on n'a pas le droit de lire : refusé."""
    manager = User.objects.create_user("rv-dm@test.io", "pw", role=RoleChoices.DEPARTMENT_MANAGER,
                                       subsidiary=sub_a)
    api.force_authenticate(manager)
    response = api.patch(reverse("vehicle-detail", args=[vehicle_a.pk]),
                         {"purchase_value": "1"}, format="json")
    assert response.status_code == 400
    vehicle_a.refresh_from_db()
    assert vehicle_a.purchase_value is None


def test_vehicle_documents_follow_the_cost_rule(api, sub_a, sub_b, fleet_a):
    """Le justificatif porte le montant : lisible au même titre que le coût, pas plus."""
    from django.core.files.base import ContentFile

    sister = Vehicle.objects.create(subsidiary=sub_b, registration="RV-B1", brand="R", model="Y")
    policy = InsurancePolicy.objects.create(
        vehicle=sister, company="NSIA", policy_number="P-RV", cost=Decimal("123456"),
        expiry_date=timezone.localdate() + timedelta(days=30),
    )
    policy.document.save("attestation-rv.pdf", ContentFile(b"%PDF-1.4 123456"), save=True)

    api.force_authenticate(fleet_a)
    row = api.get(reverse("vehicle-insurance-list"), {"vehicle": str(sister.pk)}).json()
    row = (row["results"] if isinstance(row, dict) else row)[0]
    assert row["expiry_date"], "l'échéance d'un véhicule mutualisé reste lisible"
    assert row["cost"] is None and row["document"] is None


# --- Barèmes ----------------------------------------------------------------------


def test_every_rule_change_requires_its_own_reason(api, company_admin):
    api.force_authenticate(company_admin)
    rule = api.post(reverse("trip-pricing-rule-list"), {
        "name": "RV", "amount_per_km": "200", "valid_from": "2032-01-01", "reason": "création",
    }, format="json").json()
    response = api.patch(reverse("trip-pricing-rule-detail", args=[rule["id"]]),
                         {"active": False}, format="json")
    assert response.status_code == 400 and "reason" in response.json()


def test_zero_tariff_is_refused_with_the_right_message(api, company_admin):
    api.force_authenticate(company_admin)
    response = api.post(reverse("trip-pricing-rule-list"), {
        "name": "RV0", "amount_per_km": "0", "valid_from": "2032-01-01", "reason": "test",
    }, format="json")
    assert response.status_code == 400
    assert "amount_per_km" in response.json() and "chevauche" not in response.content.decode()


# --- Dispatching et statistiques -------------------------------------------------


def test_board_never_prices_a_sister_trip_the_reader_only_takes_part_in(api, sub_a, sub_b, fleet_a):
    """Chauffeur ou demandeur d'une course d'une sœur : il la voit, il n'en lit pas le coût."""
    from apps.core.enums import ReservationStatus, TripType
    from apps.finance.models import TripPricingRule
    from apps.finance.trip_pricing import refresh_estimate
    from apps.reservations.models import Reservation
    from apps.reservations.services import _ensure_trips
    from apps.tracking.models import TripRoute

    TripPricingRule.objects.create(name="RV", amount_per_km=Decimal("200"),
                                   valid_from=timezone.localdate() - timedelta(days=1), reason="t")
    dep = timezone.now() + timedelta(hours=2)
    reservation = Reservation.objects.create(
        subsidiary=sub_b, requester=fleet_a, created_by=fleet_a, trip_date=dep.date(),
        departure_time=dep, estimated_return=dep + timedelta(hours=1), origin="Cocody",
        destination="Plateau DAKAR", purpose="Mission", passengers=1, needs_driver=False,
        trip_type=TripType.ONE_WAY, status=ReservationStatus.APPROVED,
    )
    trip = _ensure_trips(reservation)[0]
    TripRoute.objects.create(trip=trip, planned_distance_km=Decimal("5"))
    trip.refresh_from_db()
    refresh_estimate(trip)

    api.force_authenticate(fleet_a)
    rows = api.get(reverse("dispatch-board")).json()["trips"]
    row = next((r for r in rows if r["id"] == str(trip.pk)), None)
    assert row is not None, "la course reste visible de son demandeur"
    assert row.get("pricing") is None


def test_unpriced_empty_km_are_not_shown_as_free(api, fleet_a, vehicle_a):
    """Km à vide sans barème : coût inconnu (None), pas 0 XOF."""
    from apps.finance.stats import _empty_km_cost

    today = timezone.localdate()
    Vehicle.objects.filter(pk=vehicle_a.pk).update(mileage=1000)
    result = _empty_km_cost(fleet_a, {}, today, today)
    assert result["cost"] in (None, "0.00")  # 0.00 seulement s'il n'y a aucun km à vide
    if result["km"] != "0.00":
        assert result["cost"] is None


# --- Intégrité historique -------------------------------------------------------


@pytest.fixture
def priced_trip(sub_a, requester_a, fleet_a, vehicle_a):
    """Une course planifiée, tarifée à 200 XOF/km sur un itinéraire de 7,35 km (OSRM)."""
    from apps.core.enums import ReservationStatus, TripType
    from apps.finance.models import TripPricingRule
    from apps.reservations.models import Reservation
    from apps.reservations.services import _ensure_trips
    from apps.tracking.models import TripRoute
    from apps.trips.models import Trip

    TripPricingRule.objects.create(name="RV-H", amount_per_km=Decimal("200"),
                                   valid_from=timezone.localdate() - timedelta(days=5), reason="t")
    dep = timezone.now() + timedelta(hours=1)
    reservation = Reservation.objects.create(
        subsidiary=sub_a, requester=requester_a, created_by=requester_a, trip_date=dep.date(),
        departure_time=dep, estimated_return=dep + timedelta(hours=1), origin="Cocody",
        destination="Plateau", purpose="Mission", passengers=1, needs_driver=False,
        trip_type=TripType.ONE_WAY, status=ReservationStatus.APPROVED,
    )
    trip = _ensure_trips(reservation)[0]
    Trip.objects.filter(pk=trip.pk).update(vehicle=vehicle_a)
    trip.refresh_from_db()
    TripRoute.objects.create(trip=trip, planned_distance_km=Decimal("7.4"))
    trip.refresh_from_db()
    return trip


def test_estimate_uses_the_stored_distance_precision(priced_trip):
    """OSRM donne 7,35 km, la base garde 7,4 : le coût doit être celui de la distance affichée."""
    from apps.finance.models import TripPricing
    from apps.finance.trip_pricing import refresh_estimate

    priced_trip.route.planned_distance_km = Decimal("7.35")  # valeur en mémoire, non arrondie
    refresh_estimate(priced_trip)
    snapshot = TripPricing.objects.get(trip=priced_trip)
    assert snapshot.estimated_distance_km == Decimal("7.40")
    assert snapshot.estimated_cost == Decimal("1480.00")


def test_an_unmeasured_return_has_no_actual_cost(priced_trip, fleet_a, monkeypatch):
    """Sans GPS ni relevé, le compteur recopie la distance prévue : ce n'est pas une mesure."""
    from apps.finance.models import TripPricing
    from apps.trips import services as trip_services

    monkeypatch.setattr("apps.tracking.live.real_traveled_km", lambda _trip: 0.0)
    trip_services.start_trip(priced_trip, fleet_a, start_mileage=1000)
    trip_services.end_trip(priced_trip, fleet_a)  # aucun kilométrage saisi
    snapshot = TripPricing.objects.get(trip=priced_trip)
    assert snapshot.actual_distance_km is None and snapshot.actual_cost is None
    assert snapshot.estimated_cost == Decimal("1480.00"), "l'estimation, elle, reste"


def test_a_measured_odometer_is_used_when_there_is_no_gps(priced_trip, fleet_a, monkeypatch):
    from apps.finance.models import TripPricing
    from apps.trips import services as trip_services

    monkeypatch.setattr("apps.tracking.live.real_traveled_km", lambda _trip: 0.0)
    trip_services.start_trip(priced_trip, fleet_a, start_mileage=1000)
    trip_services.end_trip(priced_trip, fleet_a, end_mileage=1009)
    snapshot = TripPricing.objects.get(trip=priced_trip)
    assert (snapshot.actual_distance_km, snapshot.actual_distance_source, snapshot.actual_cost) == (
        Decimal("9.00"), "odometer", Decimal("1800.00"))


def test_a_closed_trip_with_its_frozen_cost_cannot_be_deleted(api, priced_trip, fleet_a, requester_a):
    """Supprimer la réservation emportait en cascade la course et son coût figé."""
    from django.db.models import ProtectedError

    from apps.finance.models import TripPricing
    from apps.trips import services as trip_services

    trip_services.start_trip(priced_trip, fleet_a, start_mileage=1000)
    trip_services.end_trip(priced_trip, fleet_a, end_mileage=1008)
    trip_services.close_trip(priced_trip, fleet_a)

    api.force_authenticate(requester_a)
    response = api.delete(reverse("reservation-detail", args=[priced_trip.reservation_id]))
    assert response.status_code == 400
    from django.db import transaction

    with pytest.raises(ProtectedError), transaction.atomic():
        priced_trip.reservation.delete()  # même par l'ORM ou l'admin
    assert TripPricing.objects.filter(trip=priced_trip, frozen_at__isnull=False).exists()


def test_a_missed_freeze_is_caught_up_by_the_periodic_task(priced_trip, fleet_a):
    from apps.finance.models import TripPricing
    from apps.finance.tasks import refresh_trip_pricing
    from apps.trips.models import Trip

    Trip.objects.filter(pk=priced_trip.pk).update(status="closed")  # gel jamais exécuté
    assert TripPricing.objects.get(trip=priced_trip).frozen_at is None
    assert refresh_trip_pricing()["closed_trips_frozen"] >= 1
    assert TripPricing.objects.get(trip=priced_trip).frozen_at is not None


def test_refresh_reads_the_status_in_database_not_in_memory(priced_trip):
    """Pendant un long rattrapage, la course a pu partir : son tarif ne se réécrit plus."""
    from apps.finance.models import TripPricing, TripPricingRule
    from apps.finance.trip_pricing import refresh_estimate
    from apps.trips.models import Trip

    refresh_estimate(priced_trip)
    Trip.objects.filter(pk=priced_trip.pk).update(status="in_progress")  # partie entre-temps
    TripPricingRule.objects.update(active=False)
    TripPricingRule.objects.create(name="RV-H2", amount_per_km=Decimal("999"),
                                   valid_from=timezone.localdate() - timedelta(days=5), reason="t")
    refresh_estimate(priced_trip)  # l'objet en mémoire croit encore la course « scheduled »
    assert TripPricing.objects.get(trip=priced_trip).amount_per_km == Decimal("200.00")
