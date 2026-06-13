"""Backfill des géométries PostGIS depuis les coordonnées existantes.

VehicleLocation.location / TripLocationPoint.point ← (longitude, latitude)
GeofenceZone.area ← polygone JSON [[lat, lng], ...] (anneau fermé).
Self-contained : aucune dépendance au code applicatif.
"""
from django.db import migrations


def _point(geos, latitude, longitude):
    if latitude is None or longitude is None:
        return None
    return geos.Point(float(longitude), float(latitude), srid=4326)


def _polygon(geos, polygon):
    if not polygon or len(polygon) < 3:
        return None
    ring = [(float(p[1]), float(p[0])) for p in polygon]
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    try:
        return geos.Polygon(geos.LinearRing(ring), srid=4326)
    except (ValueError, TypeError, IndexError):
        return None


def forwards(apps, schema_editor):
    from django.contrib.gis import geos

    VehicleLocation = apps.get_model("tracking", "VehicleLocation")
    TripLocationPoint = apps.get_model("tracking", "TripLocationPoint")
    GeofenceZone = apps.get_model("tracking", "GeofenceZone")

    batch = []
    for loc in VehicleLocation.objects.all().iterator():
        loc.location = _point(geos, loc.latitude, loc.longitude)
        batch.append(loc)
        if len(batch) >= 500:
            VehicleLocation.objects.bulk_update(batch, ["location"])
            batch = []
    if batch:
        VehicleLocation.objects.bulk_update(batch, ["location"])

    batch = []
    for pt in TripLocationPoint.objects.all().iterator():
        pt.point = _point(geos, pt.latitude, pt.longitude)
        batch.append(pt)
        if len(batch) >= 1000:
            TripLocationPoint.objects.bulk_update(batch, ["point"])
            batch = []
    if batch:
        TripLocationPoint.objects.bulk_update(batch, ["point"])

    for zone in GeofenceZone.objects.all().iterator():
        zone.area = _polygon(geos, zone.polygon)
        zone.save(update_fields=["area"])


def backwards(apps, schema_editor):
    VehicleLocation = apps.get_model("tracking", "VehicleLocation")
    TripLocationPoint = apps.get_model("tracking", "TripLocationPoint")
    GeofenceZone = apps.get_model("tracking", "GeofenceZone")
    VehicleLocation.objects.update(location=None)
    TripLocationPoint.objects.update(point=None)
    GeofenceZone.objects.update(area=None)


class Migration(migrations.Migration):

    dependencies = [
        ("tracking", "0004_geofencezone_area_triplocationpoint_point_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
