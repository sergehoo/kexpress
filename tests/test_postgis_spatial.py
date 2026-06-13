"""PostGIS : synchronisation des géométries + géofencing (ST_Covers) + distance (ST_Distance).

Ces tests requièrent une base PostGIS (image postgis/postgis). Ils prouvent que la
colonne spatiale est bien alimentée et que les requêtes géo passent par PostGIS.
"""
from decimal import Decimal

from django.contrib.gis.geos import Point
from django.utils import timezone

from apps.tracking.models import GeofenceZone, VehicleLocation

# Zone mission « Plateau » : lat 5.30→5.34, lng -4.045→-4.00 (ordre JSON [lat, lng]).
PLATEAU = [[5.300, -4.045], [5.300, -4.000], [5.340, -4.000], [5.340, -4.045]]


def test_geofence_zone_syncs_postgis_area(db, sub_a):
    """save() alimente `area` (Polygon) depuis le polygone JSON ; ST_Covers fonctionne."""
    z = GeofenceZone.objects.create(
        subsidiary=sub_a, name="Mission Plateau", zone_type="mission",
        polygon=PLATEAU, is_active=True,
    )
    z.refresh_from_db()
    assert z.area is not None  # géométrie synchronisée

    inside = Point(-4.02, 5.32, srid=4326)   # (lng, lat) à l'intérieur
    outside = Point(-3.90, 5.32, srid=4326)  # à l'est, hors zone
    assert GeofenceZone.objects.filter(id=z.id, area__covers=inside).exists()
    assert not GeofenceZone.objects.filter(id=z.id, area__covers=outside).exists()


def test_vehicle_location_syncs_point(db, vehicle_a):
    """save() alimente `location` (Point) en ordre (x=lng, y=lat)."""
    loc = VehicleLocation.objects.create(
        vehicle=vehicle_a, latitude=Decimal("5.32"), longitude=Decimal("-4.02"),
        recorded_at=timezone.now(),
    )
    loc.refresh_from_db()
    assert loc.location is not None
    assert abs(loc.location.x - (-4.02)) < 1e-6  # x = longitude
    assert abs(loc.location.y - 5.32) < 1e-6     # y = latitude


def test_check_geofences_detects_enter_exit_via_postgis(db, sub_a, vehicle_a):
    """Entrée puis sortie de zone détectées via ST_Covers, une alerte par transition."""
    from apps.tracking.geofence import check_geofences
    from apps.tracking.models import GeofenceAlert

    GeofenceZone.objects.create(
        subsidiary=sub_a, name="Mission Plateau", zone_type="mission",
        polygon=PLATEAU, is_active=True,
    )
    # vehicle_a appartient à sub_a — position à l'intérieur.
    loc = VehicleLocation.objects.create(
        vehicle=vehicle_a, latitude=Decimal("5.32"), longitude=Decimal("-4.02"),
        recorded_at=timezone.now(),
    )
    assert check_geofences(vehicle_a, loc) == 1
    assert GeofenceAlert.objects.filter(vehicle=vehicle_a, event="enter").count() == 1

    # Déplacement hors zone → transition de sortie (critique pour une zone mission).
    loc.latitude = Decimal("5.32")
    loc.longitude = Decimal("-3.90")
    loc.save()
    assert check_geofences(vehicle_a, loc) == 1
    assert GeofenceAlert.objects.filter(vehicle=vehicle_a, event="exit").count() == 1

    # Pas de nouvelle alerte tant que l'état persiste (toujours dehors).
    assert check_geofences(vehicle_a, loc) == 0


def test_nearby_distance_uses_postgis_meters(db, vehicle_a):
    """ST_Distance géographique renvoie une distance en mètres exploitable (km)."""
    from django.contrib.gis.db.models.functions import Distance

    from apps.vehicles.models import Vehicle

    VehicleLocation.objects.create(
        vehicle=vehicle_a, latitude=Decimal("5.35"), longitude=Decimal("-4.00"),
        recorded_at=timezone.now(),
    )
    origin = Point(-4.02, 5.32, srid=4326)
    v = (
        Vehicle.objects.filter(id=vehicle_a.id, last_location__isnull=False)
        .annotate(distance=Distance("last_location__location", origin))
        .first()
    )
    assert v is not None and v.distance is not None
    assert 2.0 < v.distance.km < 8.0  # ~4 km entre les deux points d'Abidjan
