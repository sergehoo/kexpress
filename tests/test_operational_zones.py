"""P1 — Zones opérationnelles (§3) : rattachement d'un point / d'une course à une zone.

Éprouve les trois stratégies de résolution (contenance PostGIS, rayon, proximité bornée),
leur ordre de priorité, et les deux garde-fous : seules les zones OPÉRATIONNELLES comptent,
et la résolution ne franchit jamais la frontière de filiale.
"""
import pytest
from django.core.management import call_command
from django.db import IntegrityError
from django.utils import timezone

from apps.core.enums import GeofenceType, ZoneCategory
from apps.tracking.models import GeofenceZone, TripRoute
from apps.tracking.zones import resolve_route_zones, zone_for_point

# Repères d'Abidjan utilisés par les tests.
PLATEAU = (5.3200, -4.0200)
COCODY = (5.3600, -3.9900)
DAKAR = (14.7000, -17.4000)  # à des milliers de km : hors de toute zone

# Carré autour du Plateau, au format attendu par le modèle : [[lat, lng], ...].
PLATEAU_SQUARE = [[5.31, -4.03], [5.31, -4.01], [5.33, -4.01], [5.33, -4.03]]


def _zone(sub, code, name, *, polygon=None, center=None, radius=None,
          zone_type=GeofenceType.OPERATIONAL):
    return GeofenceZone.objects.create(
        subsidiary=sub, code=code, name=name, zone_type=zone_type,
        category=ZoneCategory.ADMINISTRATIVE,
        polygon=polygon or [],
        center_lat=center[0] if center else None,
        center_lng=center[1] if center else None,
        radius_m=radius,
    )


# --- Géométrie du modèle ---------------------------------------------------


def test_center_is_derived_from_polygon(db, sub_a):
    """Une zone dessinée à la main obtient un centre automatiquement : la recherche
    « zone la plus proche » reste donc utilisable sans saisie supplémentaire."""
    zone = _zone(sub_a, "plateau", "Plateau", polygon=PLATEAU_SQUARE)
    zone.refresh_from_db()
    assert zone.center is not None
    assert float(zone.center_lat) == pytest.approx(5.32, abs=0.01)
    assert float(zone.center_lng) == pytest.approx(-4.02, abs=0.01)


def test_zone_code_is_unique_per_subsidiary(db, sub_a, sub_b):
    """Le code est la clé de semis : unique dans une filiale, réutilisable dans une autre."""
    _zone(sub_a, "plateau", "Plateau", center=PLATEAU, radius=2500)
    _zone(sub_b, "plateau", "Plateau", center=PLATEAU, radius=2500)  # autre filiale : OK
    with pytest.raises(IntegrityError):
        _zone(sub_a, "plateau", "Plateau bis", center=PLATEAU, radius=2500)


def test_blank_codes_do_not_collide(db, sub_a):
    """Les zones dessinées à l'UI n'ont pas de code : la contrainte partielle les ignore."""
    _zone(sub_a, "", "Zone 1", polygon=PLATEAU_SQUARE)
    _zone(sub_a, "", "Zone 2", polygon=PLATEAU_SQUARE)
    assert GeofenceZone.objects.filter(code="").count() == 2


# --- Résolution d'un point -------------------------------------------------


def test_polygon_containment_wins_over_radius(db, sub_a):
    """La contenance est la stratégie la plus fiable : elle primerait même sur une zone
    circulaire dont le centre est plus proche du point."""
    polygon_zone = _zone(sub_a, "plateau", "Plateau", polygon=PLATEAU_SQUARE)
    _zone(sub_a, "grand-abidjan", "Grand Abidjan", center=PLATEAU, radius=25000)
    assert zone_for_point(*PLATEAU, subsidiary_id=sub_a.pk) == polygon_zone


def test_radius_zone_matches_point_inside(db, sub_a):
    zone = _zone(sub_a, "plateau", "Plateau", center=PLATEAU, radius=2500)
    assert zone_for_point(5.3210, -4.0210, subsidiary_id=sub_a.pk) == zone


def test_point_outside_radius_falls_back_to_nearest(db, sub_a):
    """Hors de tout rayon mais à portée raisonnable : rattachement à la plus proche."""
    plateau = _zone(sub_a, "plateau", "Plateau", center=PLATEAU, radius=500)
    _zone(sub_a, "cocody", "Cocody", center=COCODY, radius=500)
    # ~1 km au nord du Plateau : hors des deux rayons (500 m), mais le Plateau reste le plus proche.
    assert zone_for_point(5.3290, -4.0200, subsidiary_id=sub_a.pk) == plateau


def test_smaller_radius_zone_wins_when_both_contain(db, sub_a):
    """Deux zones circulaires contiennent le point : la plus proche du centre gagne, donc
    la plus spécifique (le Plateau plutôt que le Grand Abidjan)."""
    plateau = _zone(sub_a, "plateau", "Plateau", center=PLATEAU, radius=2500)
    _zone(sub_a, "grand-abidjan", "Grand Abidjan", center=(5.3400, -4.0200), radius=25000)
    assert zone_for_point(*PLATEAU, subsidiary_id=sub_a.pk) == plateau


def test_far_away_point_has_no_zone(db, sub_a):
    """ADVERSARIAL — sans borne, une course à Dakar serait « rattachée » à Abidjan."""
    _zone(sub_a, "plateau", "Plateau", center=PLATEAU, radius=2500)
    assert zone_for_point(*DAKAR, subsidiary_id=sub_a.pk) is None


def test_non_operational_geofences_are_ignored(db, sub_a):
    """ADVERSARIAL — une géofence d'alerte (mission, interdite…) couvrant le point ne doit
    jamais capturer la course : elle n'a pas de sens opérationnel de dispatching."""
    _zone(sub_a, "alerte", "Zone interdite", polygon=PLATEAU_SQUARE,
          zone_type=GeofenceType.FORBIDDEN)
    assert zone_for_point(*PLATEAU, subsidiary_id=sub_a.pk) is None


def test_inactive_zone_is_ignored(db, sub_a):
    zone = _zone(sub_a, "plateau", "Plateau", polygon=PLATEAU_SQUARE)
    GeofenceZone.objects.filter(pk=zone.pk).update(is_active=False)
    assert zone_for_point(*PLATEAU, subsidiary_id=sub_a.pk) is None


def test_resolution_never_crosses_subsidiary(db, sub_a, sub_b):
    """ADVERSARIAL — isolation multi-tenant : la zone d'une autre filiale n'est jamais
    renvoyée, même si elle couvre exactement le point."""
    _zone(sub_b, "plateau", "Plateau (Dakar SA)", polygon=PLATEAU_SQUARE)
    assert zone_for_point(*PLATEAU, subsidiary_id=sub_a.pk) is None


def test_missing_coordinates_yield_no_zone(db, sub_a):
    _zone(sub_a, "plateau", "Plateau", polygon=PLATEAU_SQUARE)
    assert zone_for_point(None, None, subsidiary_id=sub_a.pk) is None


# --- Rattachement d'une course --------------------------------------------


@pytest.fixture
def route(db, sub_a, requester_a):
    """Itinéraire Cocody → Plateau, coordonnées renseignées."""
    from datetime import timedelta

    from apps.core.enums import ReservationStatus, TripType
    from apps.reservations.models import Reservation
    from apps.reservations.services import _ensure_trips

    dep = timezone.now() + timedelta(days=1)
    res = Reservation.objects.create(
        subsidiary=sub_a, requester=requester_a, created_by=requester_a,
        trip_date=dep.date(), departure_time=dep, estimated_return=dep + timedelta(hours=3),
        origin="Cocody", destination="Plateau", purpose="Mission", passengers=2,
        needs_driver=False, trip_type=TripType.ONE_WAY, status=ReservationStatus.APPROVED,
    )
    trip = _ensure_trips(res)[0]
    return TripRoute.objects.create(
        trip=trip,
        origin_label="Cocody", origin_lat=COCODY[0], origin_lng=COCODY[1],
        destination_label="Plateau", destination_lat=PLATEAU[0], destination_lng=PLATEAU[1],
    )


def test_resolve_route_zones_sets_origin_and_destination(db, sub_a, route):
    cocody = _zone(sub_a, "cocody", "Cocody", center=COCODY, radius=5000)
    plateau = _zone(sub_a, "plateau", "Plateau", polygon=PLATEAU_SQUARE)

    assert resolve_route_zones(route) is True
    route.refresh_from_db()
    assert route.origin_zone == cocody
    assert route.destination_zone == plateau


def test_resolve_route_zones_is_idempotent(db, sub_a, route):
    _zone(sub_a, "cocody", "Cocody", center=COCODY, radius=5000)
    _zone(sub_a, "plateau", "Plateau", polygon=PLATEAU_SQUARE)
    assert resolve_route_zones(route) is True
    assert resolve_route_zones(route) is False  # rien à refaire


def test_resolve_route_zones_force_recomputes(db, sub_a, route):
    """Le découpage des zones peut changer : `force` permet de re-rattacher les courses."""
    _zone(sub_a, "cocody", "Cocody", center=COCODY, radius=5000)
    _zone(sub_a, "plateau", "Plateau", polygon=PLATEAU_SQUARE)
    resolve_route_zones(route)
    route.refresh_from_db()

    GeofenceZone.objects.filter(code="plateau").delete()
    precise = _zone(sub_a, "plateau-2", "Plateau (redécoupé)", polygon=PLATEAU_SQUARE)
    assert resolve_route_zones(route, force=True) is True
    route.refresh_from_db()
    assert route.destination_zone == precise


def test_resolve_route_zones_tolerates_absence_of_zones(db, route):
    """Aucune zone semée : la course reste exploitable, sans rattachement."""
    assert resolve_route_zones(route) is False
    route.refresh_from_db()
    assert route.origin_zone_id is None and route.destination_zone_id is None


# --- Semis -----------------------------------------------------------------


def test_seed_command_is_idempotent(db, sub_a):
    call_command("seed_operational_zones", "--subsidiary", "ABJ", verbosity=0)
    first = GeofenceZone.objects.filter(subsidiary=sub_a, zone_type=GeofenceType.OPERATIONAL).count()
    assert first == 9

    call_command("seed_operational_zones", "--subsidiary", "ABJ", verbosity=0)
    assert GeofenceZone.objects.filter(subsidiary=sub_a).count() == first  # aucun doublon


def test_seeded_zones_resolve_real_points(db, sub_a):
    """Preuve de bout en bout : après semis, les repères réels tombent dans la bonne zone."""
    call_command("seed_operational_zones", "--subsidiary", "ABJ", verbosity=0)
    assert zone_for_point(*PLATEAU, subsidiary_id=sub_a.pk).code == "plateau"
    assert zone_for_point(*COCODY, subsidiary_id=sub_a.pk).code == "cocody"
    assert zone_for_point(5.2610, -3.9260, subsidiary_id=sub_a.pk).code == "aeroport"
    assert zone_for_point(*DAKAR, subsidiary_id=sub_a.pk) is None
