import pytest

from apps.accounts.models import User
from apps.core.enums import RoleChoices, VehicleStatus
from apps.organizations.models import Company, Subsidiary
from apps.vehicles.models import Vehicle


@pytest.fixture(autouse=True)
def _no_route_geocoding(settings):
    """Désactive le provisionnement réseau d'itinéraire (Nominatim/OSRM) pendant les tests.

    Le suivi appelle `ensure_trip_route` qui géocode l'origine/destination ; sans cette
    bascule, chaque test de suivi déclencherait des appels réseau (lents, flaky, soumis à
    quota). Les tests qui valident le provisionnement le réactivent et mockent le réseau.
    """
    settings.TRACKING_GEOCODE_ROUTES = False


@pytest.fixture
def company(db):
    return Company.objects.create(name="Kaydan Groupe")


@pytest.fixture
def sub_a(company):
    return Subsidiary.objects.create(company=company, name="Abidjan", code="ABJ")


@pytest.fixture
def sub_b(company):
    return Subsidiary.objects.create(company=company, name="Dakar", code="DKR")


@pytest.fixture
def company_admin(db):
    u = User.objects.create_user("admin@test.io", "pw", role=RoleChoices.COMPANY_ADMIN)
    return u


@pytest.fixture
def admin_a(sub_a):
    return User.objects.create_user(
        "a@test.io", "pw", role=RoleChoices.SUBSIDIARY_ADMIN, subsidiary=sub_a
    )


@pytest.fixture
def requester_a(sub_a):
    return User.objects.create_user(
        "req@test.io", "pw", role=RoleChoices.REQUESTER, subsidiary=sub_a
    )


@pytest.fixture
def vehicles(sub_a, sub_b):
    va = Vehicle.objects.create(subsidiary=sub_a, registration="A-1", brand="T", model="X")
    vb = Vehicle.objects.create(subsidiary=sub_b, registration="B-1", brand="R", model="Y",
                                status=VehicleStatus.AVAILABLE)
    return va, vb


# --- Fixtures Phase 2 (workflow) ----------------------------------------


@pytest.fixture
def manager_a(sub_a):
    return User.objects.create_user(
        "mgr@test.io", "pw", role=RoleChoices.DEPARTMENT_MANAGER, subsidiary=sub_a
    )


@pytest.fixture
def fleet_a(sub_a):
    return User.objects.create_user(
        "fleet@test.io", "pw", role=RoleChoices.FLEET_MANAGER, subsidiary=sub_a
    )


@pytest.fixture
def vehicle_a(sub_a):
    from apps.vehicles.models import Vehicle

    return Vehicle.objects.create(
        subsidiary=sub_a, registration="A-100", brand="Toyota", model="Hilux", capacity=5
    )


@pytest.fixture
def driver_a(sub_a):
    from apps.drivers.models import Driver

    return Driver.objects.create(
        subsidiary=sub_a, first_name="Koffi", last_name="Yao", is_available=True
    )


@pytest.fixture
def reservation(sub_a, requester_a):
    """Réservation brouillon : départ +1j, retour +1j 3h, 3 passagers, chauffeur requis."""
    from datetime import timedelta

    from django.utils import timezone

    from apps.reservations.models import Reservation

    dep = timezone.now() + timedelta(days=1)
    return Reservation.objects.create(
        subsidiary=sub_a, requester=requester_a, created_by=requester_a,
        trip_date=dep.date(), departure_time=dep, estimated_return=dep + timedelta(hours=3),
        destination="Aéroport", purpose="Mission", passengers=3, needs_driver=True,
    )
