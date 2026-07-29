"""P0 — Anti-double-booking : contraintes d'exclusion sur les courses.

Interdit AU NIVEAU BASE deux courses actives partageant le même véhicule (ou chauffeur)
sur des fenêtres prévues qui se chevauchent. La vérification applicative
(`trip_time_conflicts`) reste la première ligne (message métier), mais elle est un
check-then-write : sous READ COMMITTED deux transactions concurrentes peuvent la passer
toutes les deux. Ces contraintes rendent la collision impossible.

La migration est GARDÉE par un pré-scan : si des chevauchements existent déjà en base,
elle s'arrête avec la liste des courses fautives plutôt que d'échouer sur une violation
de contrainte opaque.
"""

import apps.core.db
import django.contrib.postgres.constraints
import django.contrib.postgres.fields.ranges
from django.conf import settings
from django.contrib.postgres.operations import BtreeGistExtension
from django.db import migrations, models

# Statuts pour lesquels une course occupe réellement son véhicule / son chauffeur.
_OCCUPYING = ["scheduled", "departed", "in_progress", "returned"]


def _prescan_overlaps(apps_registry, schema_editor):
    """Échoue avec un diagnostic lisible si des chevauchements préexistent (risque R13)."""
    table = apps_registry.get_model("trips", "Trip")._meta.db_table
    placeholders = ", ".join(["%s"] * len(_OCCUPYING))
    problems = []
    with schema_editor.connection.cursor() as cursor:
        for column, label in (("vehicle_id", "véhicule"), ("driver_id", "chauffeur")):
            cursor.execute(
                f"""
                SELECT a.id, b.id, a.{column}
                  FROM {table} a
                  JOIN {table} b
                    ON a.{column} = b.{column}
                   AND a.id < b.id
                   AND tstzrange(a.planned_departure_at, a.planned_arrival_at, '[)')
                    && tstzrange(b.planned_departure_at, b.planned_arrival_at, '[)')
                 WHERE a.{column} IS NOT NULL
                   AND a.status IN ({placeholders}) AND b.status IN ({placeholders})
                   AND a.planned_departure_at IS NOT NULL AND a.planned_arrival_at IS NOT NULL
                   AND b.planned_departure_at IS NOT NULL AND b.planned_arrival_at IS NOT NULL
                 LIMIT 25
                """,
                [*_OCCUPYING, *_OCCUPYING],
            )
            problems += [f"  · {label} {ref} — courses {a} et {b}" for a, b, ref in cursor.fetchall()]

    if problems:
        raise RuntimeError(
            "Migration interrompue : des courses actives se chevauchent DÉJÀ sur le même "
            "véhicule ou chauffeur. Corrigez-les (réaffecter, replanifier ou annuler) puis "
            "relancez la migration.\n" + "\n".join(problems)
        )


class Migration(migrations.Migration):

    dependencies = [
        ('drivers', '0004_driver_document'),
        ('organizations', '0001_initial'),
        ('reservations', '0002_reservation_return_time_reservation_trip_type'),
        ('trips', '0004_trip_planned_arrival_at_trip_planned_departure_at_and_more'),
        ('vehicles', '0005_vehicle_fuel_specs'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # `=` sur une colonne scalaire dans un index GIST exige btree_gist.
        BtreeGistExtension(),
        migrations.RunPython(_prescan_overlaps, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='trip',
            constraint=django.contrib.postgres.constraints.ExclusionConstraint(condition=models.Q(('planned_arrival_at__isnull', False), ('planned_departure_at__isnull', False), ('status__in', ['scheduled', 'departed', 'in_progress', 'returned']), ('vehicle__isnull', False)), expressions=[('vehicle', '='), (apps.core.db.TsTzRange('planned_departure_at', 'planned_arrival_at', django.contrib.postgres.fields.ranges.RangeBoundary()), '&&')], name='excl_trip_vehicle_overlap', violation_error_message='Conflit horaire : ce véhicule est déjà engagé sur ce créneau.'),
        ),
        migrations.AddConstraint(
            model_name='trip',
            constraint=django.contrib.postgres.constraints.ExclusionConstraint(condition=models.Q(('driver__isnull', False), ('planned_arrival_at__isnull', False), ('planned_departure_at__isnull', False), ('status__in', ['scheduled', 'departed', 'in_progress', 'returned'])), expressions=[('driver', '='), (apps.core.db.TsTzRange('planned_departure_at', 'planned_arrival_at', django.contrib.postgres.fields.ranges.RangeBoundary()), '&&')], name='excl_trip_driver_overlap', violation_error_message='Conflit horaire : ce chauffeur est déjà engagé sur ce créneau.'),
        ),
    ]
