"""Replanification des horaires (glisser / redimensionner une barre du planning)."""
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.core.enums import ReservationStatus, RoleChoices, TripType
from apps.organizations.models import Company, Subsidiary
from apps.reservations import services
from apps.reservations.models import Reservation
from apps.reservations.workflow import WorkflowError
from apps.vehicles.models import Vehicle


@pytest.fixture
def ctx(db):
    company = Company.objects.create(name="Kaydan")
    sub = Subsidiary.objects.create(company=company, name="Plateau", code="PLT")
    fleet = User.objects.create_user(email="fleet@k.ci", password="x", role=RoleChoices.FLEET_MANAGER, subsidiary=sub)
    req = User.objects.create_user(email="req@k.ci", password="x", role=RoleChoices.REQUESTER, subsidiary=sub)
    veh = Vehicle.objects.create(subsidiary=sub, registration="AA-1-BC", brand="Toyota", model="Hilux",
                                 status="reserved", capacity=5)
    return dict(sub=sub, fleet=fleet, req=req, veh=veh)


def _res(ctx, **kw):
    now = timezone.now() + timedelta(days=2)
    defaults = dict(
        subsidiary=ctx["sub"], requester=ctx["req"], created_by=ctx["req"],
        trip_date=now.date(), departure_time=now, estimated_return=now + timedelta(hours=2),
        origin="Siège", destination="Banque", purpose="Mission", passengers=1, needs_driver=False,
        status=ReservationStatus.VEHICLE_ASSIGNED, vehicle=ctx["veh"],
    )
    defaults.update(kw)
    return Reservation.objects.create(**defaults)


@pytest.mark.django_db
def test_reschedule_moves_window(ctx):
    r = _res(ctx)
    new_dep = r.departure_time + timedelta(hours=5)
    new_ret = new_dep + timedelta(hours=2)
    services.reschedule(r, new_dep, new_ret, ctx["fleet"])
    r.refresh_from_db()
    assert r.departure_time == new_dep
    assert r.estimated_return == new_ret
    assert r.trip_date == new_dep.date()


@pytest.mark.django_db
def test_reschedule_blocked_once_started(ctx):
    r = _res(ctx, status=ReservationStatus.IN_PROGRESS)
    with pytest.raises(WorkflowError):
        services.reschedule(r, r.departure_time + timedelta(hours=1),
                            r.estimated_return + timedelta(hours=1), ctx["fleet"])


@pytest.mark.django_db
def test_reschedule_rejects_invalid_window(ctx):
    r = _res(ctx)
    with pytest.raises(WorkflowError):  # retour avant départ
        services.reschedule(r, r.departure_time, r.departure_time - timedelta(hours=1), ctx["fleet"])


@pytest.mark.django_db
def test_reschedule_detects_vehicle_conflict(ctx):
    r1 = _res(ctx)
    other_dep = r1.departure_time + timedelta(hours=10)
    _res(ctx, departure_time=other_dep, estimated_return=other_dep + timedelta(hours=2))  # même véhicule
    with pytest.raises(WorkflowError):  # déplacer r1 sur le créneau de l'autre course
        services.reschedule(r1, other_dep, other_dep + timedelta(hours=1), ctx["fleet"])


@pytest.mark.django_db
def test_reschedule_round_trip_keeps_return_time(ctx):
    now = timezone.now() + timedelta(days=2)
    r = _res(ctx, trip_type=TripType.ROUND_TRIP, return_time=now + timedelta(hours=3),
             estimated_return=now + timedelta(hours=5))
    # On élargit la fenêtre (départ plus tôt, fin plus tard) sans toucher au départ retour.
    new_dep = now - timedelta(hours=1)
    new_ret = now + timedelta(hours=6)
    services.reschedule(r, new_dep, new_ret, ctx["fleet"])
    r.refresh_from_db()
    assert r.return_time == now + timedelta(hours=3)  # inchangé


@pytest.mark.django_db
def test_reschedule_round_trip_window_only_does_not_reject(ctx):
    """Régression (HIGH) : replanifier un aller-retour en n'envoyant que départ + fin
    (cas du planning) ne doit PAS être rejeté ; le départ retour est borné dans la fenêtre."""
    now = timezone.now() + timedelta(days=2)
    r = _res(ctx, trip_type=TripType.ROUND_TRIP, return_time=now + timedelta(hours=3),
             estimated_return=now + timedelta(hours=5))
    new_dep = now + timedelta(hours=4)   # décale toute la fenêtre, sans fournir return_time
    new_ret = now + timedelta(hours=9)
    services.reschedule(r, new_dep, new_ret, ctx["fleet"])  # ne lève pas
    r.refresh_from_db()
    assert r.departure_time == new_dep
    assert new_dep < r.return_time < new_ret  # départ retour replacé dans la nouvelle fenêtre


@pytest.mark.django_db
def test_can_reschedule_permission(ctx):
    other = User.objects.create_user(email="other@k.ci", password="x", role=RoleChoices.REQUESTER, subsidiary=ctx["sub"])
    assigned = _res(ctx)  # VEHICLE_ASSIGNED
    pending = _res(ctx, status=ReservationStatus.SUBMITTED)
    assert services.can_reschedule(assigned, ctx["fleet"]) is True    # gestionnaire (sa filiale)
    assert services.can_reschedule(assigned, ctx["req"]) is False     # demandeur, déjà affecté → non
    assert services.can_reschedule(pending, ctx["req"]) is True       # demandeur, avant affectation → oui
    assert services.can_reschedule(assigned, other) is False          # autre demandeur
