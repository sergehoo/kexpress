"""Revue F2 — immuabilité de l'historique financier : U6 à U13.

Chaque test garde UN constat de la revue F2 (cité par son identifiant) et échouerait si le
correctif était retiré : statuts, montants et lignes exacts, et une écriture refusée n'écrit
rien.

- U6  course partie dans un mois clos, figée après sa clôture : rapportée au mois de son gel ;
- U7  véhicule transféré après clôture : le mois clos reste à la filiale propriétaire d'alors ;
- U8  clôtures dans le désordre : le cumul d'un mois clos est celui figé à SA clôture ;
- U9  une réservation dont une course porte une pièce financière ne se supprime pas ;
- U10 coût de course figé : totaux, état et dates de gel verrouillés ; dates de la course
      verrouillées ; date et auteur d'une clôture verrouillés ;
- U11 l'auteur d'un ajustement et l'auteur d'une clôture ne se suppriment pas ;
- U12 le justificatif historique (`Expense.receipt`) d'une dépense validée est conservé ;
- U13 retirer un justificatif : réservé à l'auteur ou au déposant, et tracé.

Déjà couverts ailleurs, volontairement NON dupliqués ici :
- un ajustement décidé ne se modifie ni ne se supprime → `test_f2_security.py::
  test_16c_adjustments_and_history_are_never_deleted` ;
- l'historique d'une dépense ne se modifie ni ne se supprime → même test, et
  `test_f2_workflow.py::test_6c_history_rows_are_immutable`.

Mois clos de référence : mars 2025 (et avril 2025 pour U8). Le mois « en cours » est calculé
(`timezone.localdate()`), jamais codé en dur.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.db.models import ProtectedError
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.core.enums import ReservationStatus, RoleChoices, TripType
from apps.expenses.models import ElectricCharge, Expense, ExpenseStatusHistory, FuelLog
from apps.expenses.workflow import record_creation
from apps.finance.adjustments import create_adjustment
from apps.finance.cost_read import month_trip_costs, subsidiary_costs, trip_cost_rows, vehicle_month, vehicle_rows
from apps.finance.locks import FinancialHistoryLocked
from apps.finance.models import (
    CostAllocation, FinancialAdjustment, FinancialAttachment, FinancialPeriod, TripCost,
    VehicleMonthlyCost,
)
from apps.finance.periods import close_period
from apps.finance.trip_cost import apply_totals, freeze_direct
from apps.maintenance.models import MaintenanceRecord, MaintenanceType
from apps.reservations.models import Reservation
from apps.trips.models import Trip
from apps.vehicles.models import InsurancePolicy, Vehicle
from tests.test_finance_f1 import _at, _closed_trip, _user

pytestmark = pytest.mark.django_db

MARCH = (2025, 3)
APRIL = (2025, 4)
PDF = b"%PDF-1.4 justificatif revue F2"


# --- Outillage ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    """Les fichiers déposés pendant les tests ne touchent pas le vrai MEDIA_ROOT."""
    settings.MEDIA_ROOT = tmp_path


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def insured(vehicle_a):
    """Assurance annuelle 365 000 : 1 000 par jour — 31 000 en mars, 30 000 en avril."""
    InsurancePolicy.objects.create(vehicle=vehicle_a, company="NSIA", start_date=date(2025, 1, 1),
                                   expiry_date=date(2025, 12, 31), cost=Decimal("365000"))
    return vehicle_a


def _current():
    today = timezone.localdate()
    return today.year, today.month


def _label(year, month):
    return f"{year}-{month:02d}"


def _rows(response):
    body = response.json()
    return body["results"] if isinstance(body, dict) and "results" in body else body


def _resave(instance, **changes):
    for field, value in changes.items():
        setattr(instance, field, value)
    instance.save()


def _locked(action):
    """L'écriture est refusée par le verrou d'historique, dans un point de sauvegarde (la
    transaction du test reste utilisable)."""
    with pytest.raises(FinancialHistoryLocked), transaction.atomic():
        action()


def _trip_cost_row(trip):
    return TripCost.objects.filter(trip=trip).values().get()


def _pdf(name="recu.pdf"):
    return SimpleUploadedFile(name, PDF, content_type="application/pdf")


# =====================================================================================
# U6 — course partie dans un mois clos, figée après la clôture
# =====================================================================================


def test_u6_trip_frozen_after_its_month_closed_is_reported_in_its_freeze_month(
        api, sub_a, requester_a, fleet_a, company_admin, insured):
    """Revue F2 — U6 : une course partie en mars, encore en cours à la clôture de mars, puis
    clôturée et figée après coup, ne réécrit pas mars ; son coût est rapporté au mois de son
    gel (la période ouverte), et seulement là."""
    on_time = _closed_trip(sub_a, requester_a, insured, day=date(2025, 3, 5), km="100")
    late = _closed_trip(sub_a, requester_a, insured, day=date(2025, 3, 20), km="50", freeze=False)
    Trip.objects.filter(pk=late.pk).update(status="in_progress", actual_return=None)

    close_period(*MARCH, company_admin)
    assert not TripCost.objects.filter(trip=late).exists(), "une course en cours ne se fige pas"
    march_before = subsidiary_costs(company_admin, *MARCH, sub_a.pk)
    assert march_before["trips_count"] == 1
    march_rows_before = trip_cost_rows(company_admin, *MARCH)
    assert [r["trip"] for r in march_rows_before] == [str(on_time.pk)]

    # La course rentre et se clôture des mois plus tard ; son plein est saisi à ce moment-là.
    Trip.objects.filter(pk=late.pk).update(status="closed", actual_return=_at(date(2025, 3, 20), 10))
    late.refresh_from_db()
    FuelLog.objects.create(vehicle=insured, subsidiary=sub_a, trip=late, date=timezone.localdate(),
                           liters="40", amount="32000")
    freeze_direct(late)
    cost = TripCost.objects.get(trip=late)
    assert cost.direct_frozen_at is not None and cost.full_cost == Decimal("32000.00")
    assert cost.direct_frozen_at > FinancialPeriod.objects.get(year=2025, month=3).closed_at

    # Mars : chiffres identiques, la course tardive n'y figure pas.
    assert subsidiary_costs(company_admin, *MARCH, sub_a.pk) == march_before
    assert list(month_trip_costs(*MARCH).values_list("trip_id", flat=True)) == [on_time.pk]
    assert trip_cost_rows(company_admin, *MARCH) == march_rows_before

    # Mois du gel (période ouverte) : la course y est rapportée, avec son coût.
    current = _current()
    assert list(month_trip_costs(*current).values_list("trip_id", flat=True)) == [late.pk]
    assert [r["trip"] for r in trip_cost_rows(company_admin, *current)] == [str(late.pk)]
    now_costs = subsidiary_costs(company_admin, *current, sub_a.pk)
    assert (now_costs["trips_count"], now_costs["trips_cost"]) == (1, "32000.00")
    # Et l'API sert la même chose au gestionnaire de flotte de la filiale.
    api.force_authenticate(fleet_a)
    rows = _rows(api.get(f"/api/finance/trip-cost-sheets/?period={_label(*current)}"))
    assert [(r["trip"], r["full_cost"]) for r in rows] == [(str(late.pk), "32000.00")]
    rows = _rows(api.get("/api/finance/trip-cost-sheets/?period=2025-03"))
    assert [r["trip"] for r in rows] == [str(on_time.pk)]


# =====================================================================================
# U7 — transfert d'un véhicule après la clôture
# =====================================================================================


def test_u7_vehicle_transfer_after_close_leaves_the_closed_month_to_its_former_owner(
        api, sub_a, sub_b, requester_a, fleet_a, company_admin, insured):
    """Revue F2 — U7 : un véhicule de A, transféré à B après la clôture de mars, n'emporte pas
    les coûts publiés de mars : A garde ses charges indirectes, B n'en reçoit aucune ; la ligne
    de mars reste listée sous A seulement, lisible par A, introuvable pour B."""
    fin_a = _user("fin-a-u7@test.io", RoleChoices.FINANCE, sub_a)
    fleet_b = _user("fleet-b-u7@test.io", RoleChoices.FLEET_MANAGER, sub_b)
    fin_b = _user("fin-b-u7@test.io", RoleChoices.FINANCE, sub_b)
    _closed_trip(sub_a, requester_a, insured, km="100")
    close_period(*MARCH, company_admin)
    before = {sub: subsidiary_costs(company_admin, *MARCH, sub.pk) for sub in (sub_a, sub_b)}
    assert before[sub_a]["indirect_charges"] == "31000.00"
    assert before[sub_a]["under_utilisation_cost"] == "29450.00"
    assert before[sub_b]["indirect_charges"] is None

    api.force_authenticate(company_admin)
    response = api.patch(f"/api/vehicles/{insured.pk}/", {"subsidiary": str(sub_b.pk)}, format="json")
    assert response.status_code == 200, response.content
    insured.refresh_from_db()
    assert insured.subsidiary_id == sub_b.pk, "le transfert n'a pas eu lieu : test sans objet"

    # Chiffres de mars : identiques pour les deux filiales.
    for sub in (sub_a, sub_b):
        assert subsidiary_costs(company_admin, *MARCH, sub.pk) == before[sub], sub.name
    assert VehicleMonthlyCost.objects.get(vehicle=insured, period__year=2025, period__month=3).subsidiary_id == sub_a.pk
    # Liste des véhicules de mars : sous A seulement.
    assert [r["vehicle"] for r in vehicle_rows(company_admin, *MARCH, sub_a.pk)] == [str(insured.pk)]
    assert vehicle_rows(company_admin, *MARCH, sub_b.pk) == []
    group = vehicle_rows(company_admin, *MARCH)
    assert [(r["vehicle"], r["subsidiary"]) for r in group] == [(str(insured.pk), str(sub_a.pk))]
    api.force_authenticate(fleet_a)
    assert [r["vehicle"] for r in _rows(api.get("/api/finance/vehicle-costs/?period=2025-03"))] == [str(insured.pk)]
    api.force_authenticate(fleet_b)
    assert _rows(api.get("/api/finance/vehicle-costs/?period=2025-03")) == []

    # Fiche du véhicule pour mars : A la lit (filiale de la clôture), B ne la voit pas.
    url = f"/api/finance/vehicles/{insured.pk}/costs/?period=2025-03"
    for user in (fleet_a, fin_a):
        api.force_authenticate(user)
        response = api.get(url)
        assert response.status_code == 200, f"{user.email} → {response.status_code}"
        body = response.json()
        assert (body["subsidiary"], body["subsidiary_name"]) == (str(sub_a.pk), sub_a.name)
        assert body["fixed_cost"] == "31000.00" and body["provisional"] is False
    for user in (fleet_b, fin_b):
        api.force_authenticate(user)
        assert api.get(url).status_code == 404, user.email

    # Contrôle : sur le mois OUVERT, le véhicule appartient bien à B désormais — le 404 de mars
    # tient à la ligne figée, pas à un refus général.
    current = f"/api/finance/vehicles/{insured.pk}/costs/?period={_label(*_current())}"
    api.force_authenticate(fleet_b)
    response = api.get(current)
    assert response.status_code == 200 and response.json()["subsidiary"] == str(sub_b.pk)
    api.force_authenticate(fleet_a)
    assert api.get(current).status_code == 404


# =====================================================================================
# U8 — clôtures dans le désordre
# =====================================================================================


def test_u8_cumulative_cost_is_frozen_at_close_even_when_months_close_out_of_order(
        sub_a, requester_a, company_admin, insured):
    """Revue F2 — U8 : avril clos AVANT mars. Le cumul d'avril est celui calculé à SA clôture
    (`VehicleMonthlyCost.cumulative_cost`) ; clore mars ensuite ne le réécrit pas."""
    _closed_trip(sub_a, requester_a, insured, day=date(2025, 3, 10), km="100")
    _closed_trip(sub_a, requester_a, insured, day=date(2025, 4, 10), km="100")

    close_period(*APRIL, company_admin)
    april_before = vehicle_month(insured, *APRIL)
    assert april_before["cumulative_cost"] == "30000.00" and april_before["total_cost"] == "30000.00"
    assert april_before["provisional"] is False

    close_period(*MARCH, company_admin)
    march = vehicle_month(insured, *MARCH)
    assert (march["total_cost"], march["cumulative_cost"]) == ("31000.00", "31000.00")
    april_after = vehicle_month(insured, *APRIL)
    assert april_after["cumulative_cost"] == "30000.00", "clore mars a réécrit le cumul d'avril"
    assert april_after == april_before
    row = VehicleMonthlyCost.objects.get(vehicle=insured, period__year=2025, period__month=4)
    assert row.cumulative_cost == Decimal("30000.00")


# =====================================================================================
# U9 — suppression d'une réservation porteuse de pièces financières
# =====================================================================================


def _round_trip(sub, requester, vehicle):
    """Réservation aller-retour approuvée (2 courses planifiées) sur `vehicle`."""
    from apps.reservations.services import _ensure_trips

    dep = timezone.now() + timedelta(days=2)
    reservation = Reservation.objects.create(
        subsidiary=sub, requester=requester, created_by=requester, trip_date=dep.date(),
        departure_time=dep, return_time=dep + timedelta(hours=2), estimated_return=dep + timedelta(hours=3),
        origin="Cocody", destination="Plateau", purpose="Mission", passengers=2, needs_driver=False,
        trip_type=TripType.ROUND_TRIP, status=ReservationStatus.APPROVED,
    )
    trips = _ensure_trips(reservation)
    assert len(trips) == 2
    Trip.objects.filter(pk__in=[t.pk for t in trips]).update(vehicle=vehicle)
    return reservation, trips


def _financial_record(kind, sub, author, vehicle, trip):
    today = timezone.localdate()
    if kind == "expense":
        return Expense.objects.create(subsidiary=sub, trip=trip, category="toll", label="Péage",
                                      amount="1500", date=today, status="draft", created_by=author)
    if kind == "fuel_log":
        return FuelLog.objects.create(vehicle=vehicle, subsidiary=sub, trip=trip, date=today,
                                      liters="10", amount="8000")
    if kind == "electric_charge":
        return ElectricCharge.objects.create(vehicle=vehicle, subsidiary=sub, trip=trip, date=today,
                                             kwh_recharged="20", amount="3000")
    return MaintenanceRecord.objects.create(
        vehicle=vehicle, subsidiary=sub, trip=trip, nature="corrective",
        maintenance_type=MaintenanceType.objects.create(name="Réparation U9"))


@pytest.mark.parametrize("kind", ["expense", "fuel_log", "electric_charge", "maintenance"])
def test_u9_reservation_whose_trip_carries_a_financial_record_cannot_be_deleted(
        api, kind, sub_a, requester_a, fleet_a, admin_a, vehicle_a):
    """Revue F2 — U9 : DELETE d'une réservation dont une course — même ANNULÉE — porte une
    dépense, un plein, une recharge ou une maintenance est refusé (400) : la cascade aurait
    détaché la pièce de sa course (SET_NULL en masse, hors des verrous d'historique)."""
    reservation, (outbound, back) = _round_trip(sub_a, requester_a, vehicle_a)
    Trip.objects.filter(pk=back.pk).update(status="cancelled")
    record = _financial_record(kind, sub_a, fleet_a, vehicle_a, back)
    model = type(record)

    api.force_authenticate(admin_a)
    response = api.delete(f"/api/reservations/{reservation.pk}/")
    assert response.status_code == 400, response.content
    assert Reservation.objects.filter(pk=reservation.pk).exists()
    assert set(Trip.objects.filter(reservation=reservation).values_list("pk", "status")) == {
        (outbound.pk, "scheduled"), (back.pk, "cancelled")}
    assert model._base_manager.get(pk=record.pk).trip_id == back.pk, "la pièce a perdu sa course"


def test_u9b_reservation_without_financial_record_can_still_be_deleted(
        api, sub_a, requester_a, admin_a, vehicle_a):
    """Revue F2 — U9 (contrôle) : sans pièce financière, la suppression reste permise — le
    refus ci-dessus tient bien aux pièces, pas à la réservation."""
    reservation, trips = _round_trip(sub_a, requester_a, vehicle_a)
    Trip.objects.filter(pk=trips[1].pk).update(status="cancelled")
    api.force_authenticate(admin_a)
    response = api.delete(f"/api/reservations/{reservation.pk}/")
    assert response.status_code == 204, response.content
    assert not Reservation.objects.filter(pk=reservation.pk).exists()
    assert not Trip.objects.filter(pk__in=[t.pk for t in trips]).exists()


# =====================================================================================
# U10 — coût de course figé, dates de la course, clôture du mois
# =====================================================================================


def test_u10_frozen_trip_cost_totals_status_and_freeze_dates_are_locked(
        sub_a, requester_a, company_admin, insured):
    """Revue F2 — U10 : une fois le direct figé, ni les totaux (coût complet, coût/km), ni
    l'état, ni `missing`, ni la date de gel ne bougent ; seule la clôture du mois impute
    l'indirect et met les totaux à jour — UNE fois, après quoi tout est verrouillé."""
    trip = _closed_trip(sub_a, requester_a, insured, km="100")
    cost = TripCost.objects.get(trip=trip)
    assert (cost.status, cost.full_cost, cost.indirect_frozen_at) == (TripCost.DIRECT_FROZEN, Decimal("0.00"), None)
    direct = _trip_cost_row(trip)
    now = timezone.now()
    for field, value in (("full_cost", Decimal("1")), ("cost_per_km", Decimal("1")),
                         ("status", TripCost.PENDING), ("missing", ["réécrit"]),
                         ("direct_frozen_at", now), ("direct_frozen_at", None)):
        _locked(lambda: _resave(TripCost.objects.get(trip=trip), **{field: value}))
        assert _trip_cost_row(trip) == direct, f"{field} réécrit"
    TripCost.objects.get(trip=trip).save()  # contrôle : une sauvegarde sans changement passe

    # La clôture du mois, elle, impute l'indirect et met les totaux à jour.
    period, _ = close_period(*MARCH, company_admin)
    cost = TripCost.objects.get(trip=trip)
    assert cost.status == TripCost.COMPLETE and cost.period_id == period.pk
    assert cost.indirect_frozen_at == period.closed_at
    assert (cost.insurance_cost, cost.total_indirect) == (Decimal("1550.00"), Decimal("1550.00"))
    assert (cost.full_cost, cost.cost_per_km) == (Decimal("1550.00"), Decimal("15.50"))
    assert cost.direct_frozen_at == direct["direct_frozen_at"] and cost.total_direct == direct["total_direct"]
    complete = _trip_cost_row(trip)
    later = timezone.now()
    for field, value in (("full_cost", Decimal("1")), ("cost_per_km", Decimal("1")),
                         ("status", TripCost.DIRECT_FROZEN), ("missing", []),
                         ("indirect_frozen_at", None), ("indirect_frozen_at", later),
                         ("direct_frozen_at", None)):
        _locked(lambda: _resave(TripCost.objects.get(trip=trip), **{field: value}))
        assert _trip_cost_row(trip) == complete, f"{field} réécrit après clôture"

    # Une seconde imputation (rejouer le calcul de clôture) est refusée.
    def reimpute():
        again = TripCost.objects.get(trip=trip)
        again.insurance_cost = Decimal("3100.00")
        again.indirect_frozen_at = later
        apply_totals(again)
        again.save()

    _locked(reimpute)
    assert _trip_cost_row(trip) == complete
    assert sum(a.amount for a in CostAllocation.objects.filter(period=period, trip=trip)) == Decimal("1550.00")


def test_u10b_dates_of_a_trip_with_a_frozen_cost_cannot_move(sub_a, requester_a, insured):
    """Revue F2 — U10 : les dates d'une course au coût figé rangent ce coût dans un mois —
    `actual_departure` et `planned_departure_at` ne se modifient plus. Une course non figée,
    elle, reste replanifiable."""
    trip = _closed_trip(sub_a, requester_a, insured, day=date(2025, 3, 10))
    before = Trip.objects.filter(pk=trip.pk).values("actual_departure", "planned_departure_at").get()
    assert before["planned_departure_at"] is not None

    def move(field, delta):
        row = Trip.objects.get(pk=trip.pk)
        setattr(row, field, getattr(row, field) + delta)
        row.save()

    _locked(lambda: move("actual_departure", timedelta(days=30)))
    _locked(lambda: move("planned_departure_at", -timedelta(hours=1)))
    assert Trip.objects.filter(pk=trip.pk).values("actual_departure", "planned_departure_at").get() == before

    # Contrôle : la même course, sans coût figé, se déplace.
    loose = _closed_trip(sub_a, requester_a, insured, day=date(2025, 3, 12), freeze=False)
    row = Trip.objects.get(pk=loose.pk)
    row.actual_departure = row.actual_departure + timedelta(hours=1)
    row.save()
    assert Trip.objects.get(pk=loose.pk).actual_departure == _at(date(2025, 3, 12), 9)


def test_u10c_closed_period_closing_date_and_author_cannot_change(company_admin):
    """Revue F2 — U10 : un mois clos ne se rouvre pas (déjà couvert) ET sa clôture ne se
    réécrit pas : ni `closed_at`, ni `closed_by` (ni effacé, ni remplacé)."""
    other = _user("autre-cloture-u10@test.io", RoleChoices.COMPANY_ADMIN)
    period, _ = close_period(*MARCH, company_admin)
    closed = FinancialPeriod.objects.filter(pk=period.pk).values().get()
    for field, value in (("closed_at", timezone.now()), ("closed_at", None),
                         ("closed_by", other), ("closed_by", None)):
        _locked(lambda: _resave(FinancialPeriod.objects.get(pk=period.pk), **{field: value}))
        assert FinancialPeriod.objects.filter(pk=period.pk).values().get() == closed, field
    FinancialPeriod.objects.get(pk=period.pk).save()  # contrôle : sans changement, ça passe


# =====================================================================================
# U11 — suppression de l'auteur d'un ajustement ou d'une clôture
# =====================================================================================


def test_u11_author_of_an_adjustment_or_of_a_close_cannot_be_hard_deleted(api, settings, sub_a):
    """Revue F2 — U11 : supprimer définitivement le compte de l'auteur d'un ajustement ou de
    celui qui a clos un mois effacerait « qui » de la piste d'audit financière : refusé par
    l'ORM (ProtectedError) comme par l'API (409), le compte et les liens restent."""
    settings.KEYCLOAK_ADMIN_ENABLED = False
    closer = _user("closer-u11@test.io", RoleChoices.FINANCE)  # Finance groupe : clôture
    author = _user("author-u11@test.io", RoleChoices.FINANCE, sub_a)
    root = _user("root-u11@test.io", RoleChoices.SUPER_ADMIN)
    free = _user("free-u11@test.io", RoleChoices.FLEET_MANAGER, sub_a)
    period, _ = close_period(*MARCH, closer)
    adjustment = create_adjustment(author=author, original=MARCH, amount=Decimal("900"),
                                   reason="Facture de mars oubliée", subsidiary_id=sub_a.pk, source="other")

    # Le refus tient EXACTEMENT à l'ajustement / à la clôture (aucune autre ligne protégée ne
    # le provoquerait à leur place) : sans PROTECT sur ces liens, la suppression passerait.
    for user, protected in ((author, {adjustment}), (closer, {period})):
        with pytest.raises(ProtectedError) as exc, transaction.atomic():
            User.objects.get(pk=user.pk).delete()
        assert set(exc.value.protected_objects) == protected, user.email
    api.force_authenticate(root)
    for user in (author, closer):
        response = api.delete(f"/api/employees/{user.pk}/?hard=1")
        assert response.status_code == 409, f"{user.email} → {response.status_code} {response.content[:200]}"
        assert User.objects.filter(pk=user.pk, is_active=True).exists()
    assert FinancialAdjustment.objects.get(pk=adjustment.pk).created_by_id == author.pk
    assert FinancialPeriod.objects.get(pk=period.pk).closed_by_id == closer.pk

    # Contrôle : un compte sans historique financier se supprime bien définitivement.
    assert api.delete(f"/api/employees/{free.pk}/?hard=1").status_code == 204
    assert not User.objects.filter(pk=free.pk).exists()


# =====================================================================================
# U12 — justificatif historique (`Expense.receipt`) d'une dépense validée
# =====================================================================================


@pytest.mark.parametrize("status", ["validated", "paid"])
def test_u12_legacy_receipt_of_a_validated_expense_cannot_be_cleared_or_replaced(sub_a, fleet_a, status):
    """Revue F2 — U12 : le justificatif porté par le champ historique `Expense.receipt` d'une
    dépense validée ou payée est conservé — ni effacé, ni remplacé (FinancialHistoryLocked),
    comme les justificatifs `FinancialAttachment`."""
    expense = Expense.objects.create(subsidiary=sub_a, category="toll", label="Péage justifié",
                                     amount="1500", date=timezone.localdate(), status=status,
                                     created_by=fleet_a, receipt=_pdf("recu.pdf"))
    kept = Expense.objects.get(pk=expense.pk).receipt.name
    assert kept and expense.receipt.storage.exists(kept)

    _locked(lambda: _resave(Expense.objects.get(pk=expense.pk), receipt=None))
    _locked(lambda: _resave(Expense.objects.get(pk=expense.pk), receipt=""))
    _locked(lambda: _resave(Expense.objects.get(pk=expense.pk), receipt=_pdf("remplace.pdf")))
    row = Expense.objects.get(pk=expense.pk)
    assert row.receipt.name == kept and row.status == status
    assert row.receipt.storage.exists(kept)


def test_u12b_legacy_receipt_of_a_draft_stays_editable(sub_a, fleet_a):
    """Revue F2 — U12 (contrôle) : tant que la dépense est en brouillon, son justificatif se
    remplace et s'efface — le verrou tient bien à la validation."""
    expense = Expense.objects.create(subsidiary=sub_a, category="toll", label="Péage brouillon",
                                     amount="1500", date=timezone.localdate(), status="draft",
                                     created_by=fleet_a, receipt=_pdf("recu.pdf"))
    _resave(Expense.objects.get(pk=expense.pk), receipt=_pdf("remplace.pdf"))
    replaced = Expense.objects.get(pk=expense.pk).receipt.name
    assert replaced and "remplace" in replaced
    _resave(Expense.objects.get(pk=expense.pk), receipt=None)
    assert not Expense.objects.get(pk=expense.pk).receipt


# =====================================================================================
# U13 — retrait d'un justificatif : auteur ou déposant, et tracé
# =====================================================================================


def _attach(expense, user, name):
    return FinancialAttachment.objects.create(
        expense=expense, kind="receipt", file=_pdf(name), original_name=name,
        content_type="application/pdf", size=len(PDF), uploaded_by=user)


def _trace(expense):
    return (list(ExpenseStatusHistory.objects.filter(expense=expense, action="attachment_delete")
                 .values_list("user_id", "from_status", "to_status", "details")),
            list(AuditLog.objects.filter(target_id=str(expense.pk), changes__action="expense_attachment_delete")
                 .values_list("actor_id", "changes__attachment")))


def test_u13_only_the_author_or_the_uploader_removes_an_attachment_and_it_is_traced(
        api, sub_a, fleet_a, admin_a):
    """Revue F2 — U13 : un collègue de la filiale, habilité à saisir mais ni auteur de la
    dépense ni déposant du justificatif, ne le retire pas (403, fichier conservé, rien de
    tracé). L'auteur le retire (204) : ligne d'historique `attachment_delete` et entrée
    d'audit `expense_attachment_delete`. Le déposant aussi."""
    colleague = _user("fleet2-u13@test.io", RoleChoices.FLEET_MANAGER, sub_a)
    expense = Expense.objects.create(subsidiary=sub_a, category="toll", label="Péage U13", amount="1500",
                                     date=timezone.localdate(), status="draft", created_by=fleet_a)
    record_creation(expense, fleet_a)
    first = _attach(expense, admin_a, "premier.pdf")   # déposé par l'admin, pas par l'auteur
    second = _attach(expense, admin_a, "second.pdf")
    url = f"/api/expenses/{expense.pk}/attachments/{{}}/"

    api.force_authenticate(colleague)
    response = api.delete(url.format(first.pk))
    assert response.status_code == 403, response.content
    kept = FinancialAttachment.objects.get(pk=first.pk)
    assert kept.file.storage.exists(kept.file.name)
    assert _trace(expense) == ([], [])

    # L'auteur de la dépense retire le justificatif d'un autre : permis, et tracé.
    api.force_authenticate(fleet_a)
    assert api.delete(url.format(first.pk)).status_code == 204
    assert not FinancialAttachment.objects.filter(pk=first.pk).exists()
    history, audit = _trace(expense)
    assert len(history) == 1
    user_id, from_status, to_status, details = history[0]
    assert (user_id, from_status, to_status) == (fleet_a.pk, "draft", "draft")
    assert (details["attachment"], details["name"]) == (str(first.pk), "premier.pdf")
    assert audit == [(fleet_a.pk, str(first.pk))]

    # Le déposant retire le sien.
    api.force_authenticate(admin_a)
    assert api.delete(url.format(second.pk)).status_code == 204
    history, audit = _trace(expense)
    assert [(h[0], h[3]["attachment"]) for h in history] == [(fleet_a.pk, str(first.pk)),
                                                             (admin_a.pk, str(second.pk))]
    assert sorted(audit) == sorted([(fleet_a.pk, str(first.pk)), (admin_a.pk, str(second.pk))])
