"""Stats par filiale : endpoint détail, compteurs de liste, gating des coûts, isolation."""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.enums import ReservationStatus, RoleChoices, TripStatus
from apps.expenses.models import Expense, FuelLog
from apps.organizations.models import Company, Subsidiary
from apps.reservations.models import Reservation
from apps.trips.models import Trip
from apps.vehicles.models import Vehicle


@pytest.fixture
def ctx(db):
    company = Company.objects.create(name="Kaydan")
    a = Subsidiary.objects.create(company=company, name="Plateau", code="PLT")
    b = Subsidiary.objects.create(company=company, name="Cocody", code="COC")
    admin = User.objects.create_user(email="adm@k.ci", password="x", role=RoleChoices.COMPANY_ADMIN)
    requester = User.objects.create_user(email="req@k.ci", password="x", role=RoleChoices.REQUESTER, subsidiary=a)
    now = timezone.now()
    # Parc filiale A
    v1 = Vehicle.objects.create(subsidiary=a, registration="AA-1", brand="Toyota", model="Hilux", status="available")
    Vehicle.objects.create(subsidiary=a, registration="AA-2", brand="Renault", model="Duster", status="maintenance")
    # Activité filiale A
    res = Reservation.objects.create(
        subsidiary=a, requester=requester, trip_date=now.date(), departure_time=now,
        estimated_return=now + timedelta(hours=2), destination="Bouaké", purpose="x",
        status=ReservationStatus.PENDING_FLEET,
    )
    Trip.objects.create(subsidiary=a, reservation=res, requester=requester, vehicle=v1,
                        destination="Bouaké", status=TripStatus.IN_PROGRESS, distance_km=Decimal("120.0"))
    FuelLog.objects.create(subsidiary=a, vehicle=v1, date=now.date(), liters=Decimal("40"), amount=Decimal("26000"))
    Expense.objects.create(subsidiary=a, label="Péage", category="other", amount=Decimal("5000"), date=now.date())
    client = APIClient()
    client.force_authenticate(admin)
    return dict(company=company, a=a, b=b, admin=admin, requester=requester, client=client)


@pytest.mark.django_db
def test_stats_endpoint_numbers_and_costs(ctx):
    r = ctx["client"].get(f"/api/subsidiaries/{ctx['a'].id}/stats/")
    assert r.status_code == 200, r.content
    s = r.json()
    assert s["vehicles"]["total"] == 2 and s["vehicles"]["available"] == 1 and s["vehicles"]["maintenance"] == 1
    assert s["trips"]["in_progress"] == 1 and s["trips"]["distance_km"] == 120.0
    assert s["reservations"]["pending"] == 1
    assert s["costs"]["can_see_costs"] is True
    assert s["costs"]["fuel"] == 26000.0 and s["costs"]["expenses"] == 5000.0
    assert s["costs"]["total"] == 31000.0  # fuel + maintenance(0) + expenses
    assert s["alerts"]["immobilized"] == 1


@pytest.mark.django_db
def test_costs_hidden_for_non_privileged(ctx):
    c = APIClient()
    c.force_authenticate(ctx["requester"])  # REQUESTER : pas d'accès aux coûts
    # Le requester n'a accès qu'à SA filiale (A) en lecture.
    r = c.get(f"/api/subsidiaries/{ctx['a'].id}/stats/")
    assert r.status_code == 200, r.content
    costs = r.json()["costs"]
    assert costs["can_see_costs"] is False
    assert "total" not in costs and "fuel" not in costs  # montants masqués
    assert "fuel_liters" in costs  # litres restent visibles


@pytest.mark.django_db
def test_stats_scoping_other_subsidiary_404(ctx):
    fm = User.objects.create_user(email="fm@k.ci", password="x", role=RoleChoices.FLEET_MANAGER, subsidiary=ctx["a"])
    c = APIClient()
    c.force_authenticate(fm)
    r = c.get(f"/api/subsidiaries/{ctx['b'].id}/stats/")  # filiale hors périmètre
    assert r.status_code == 404


@pytest.mark.django_db
def test_list_includes_compact_stats(ctx):
    r = ctx["client"].get("/api/subsidiaries/")
    assert r.status_code == 200
    rows = r.json()["results"]
    a_row = next(x for x in rows if x["id"] == str(ctx["a"].id))
    assert a_row["stats"]["vehicles"] == 2
    assert a_row["stats"]["trips_in_progress"] == 1
    assert a_row["stats"]["reservations_pending"] == 1
