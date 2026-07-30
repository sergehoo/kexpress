"""Détecte les chevauchements véhicule/chauffeur sur les courses actives.

À lancer AVANT d'appliquer la migration anti-double-booking sur une base existante : cette
migration crée une contrainte d'exclusion et échouerait, avec un message opaque, si des
chevauchements préexistent. La commande donne la liste exacte des courses à corriger.

Reste utile ensuite comme vérification périodique : la contrainte rend le défaut impossible,
mais un contrôle explicite permet de le prouver plutôt que de le supposer.

    python manage.py check_dispatch_overlaps           # sortie non nulle s'il y a un conflit
    python manage.py check_dispatch_overlaps --quiet   # pour un enchaînement de déploiement

⚠ La règle appliquée est celle en vigueur : deux courses d'une MÊME tournée partagent
légitimement un véhicule (covoiturage). Le discriminant est donc `COALESCE(dispatch_group,
id)` — exactement celui de la contrainte, afin que la commande ne signale jamais un
regroupement valide comme une anomalie.
"""
from django.core.management.base import BaseCommand
from django.db import connection

from apps.core.enums import TripStatus

#: Statuts pour lesquels une course occupe réellement son véhicule / son chauffeur.
OCCUPYING = [
    TripStatus.SCHEDULED, TripStatus.DEPARTED, TripStatus.IN_PROGRESS, TripStatus.RETURNED,
]

RESOURCES = (("vehicle_id", "véhicule"), ("driver_id", "chauffeur"))


def find_overlaps(limit: int = 200) -> list[dict]:
    """Paires de courses actives qui se chevauchent sur une même ressource, hors tournée."""
    from apps.trips.models import Trip

    table = Trip._meta.db_table
    placeholders = ", ".join(["%s"] * len(OCCUPYING))
    rows = []
    with connection.cursor() as cursor:
        for column, label in RESOURCES:
            cursor.execute(
                f"""
                SELECT a.id, b.id, a.{column},
                       a.planned_departure_at, a.planned_arrival_at,
                       b.planned_departure_at, b.planned_arrival_at,
                       a.destination, b.destination
                  FROM {table} a
                  JOIN {table} b
                    ON a.{column} = b.{column}
                   AND a.id < b.id
                   AND tstzrange(a.planned_departure_at, a.planned_arrival_at, '[)')
                    && tstzrange(b.planned_departure_at, b.planned_arrival_at, '[)')
                   -- Exception de tournée : même groupe = covoiturage légitime.
                   AND COALESCE(a.dispatch_group, a.id) <> COALESCE(b.dispatch_group, b.id)
                 WHERE a.{column} IS NOT NULL
                   AND a.status IN ({placeholders}) AND b.status IN ({placeholders})
                   AND a.planned_departure_at IS NOT NULL AND a.planned_arrival_at IS NOT NULL
                   AND b.planned_departure_at IS NOT NULL AND b.planned_arrival_at IS NOT NULL
                 ORDER BY a.planned_departure_at
                 LIMIT %s
                """,
                [*OCCUPYING, *OCCUPYING, limit],
            )
            for record in cursor.fetchall():
                rows.append({
                    "resource": label, "resource_id": str(record[2]),
                    "trip_a": str(record[0]), "trip_b": str(record[1]),
                    "window_a": (record[3], record[4]), "window_b": (record[5], record[6]),
                    "destination_a": record[7], "destination_b": record[8],
                })
    return rows


class Command(BaseCommand):
    help = "Détecte les chevauchements véhicule/chauffeur (à lancer avant la migration)."

    def add_arguments(self, parser):
        parser.add_argument("--quiet", action="store_true",
                            help="N'affiche que le verdict (utile en script de déploiement).")
        parser.add_argument("--limit", type=int, default=200,
                            help="Nombre maximal de conflits listés (défaut : 200).")

    def handle(self, *args, **options):
        overlaps = find_overlaps(options["limit"])
        if not overlaps:
            self.stdout.write(self.style.SUCCESS(
                "Aucun chevauchement : la migration anti-double-booking peut être appliquée."
            ))
            return

        if not options["quiet"]:
            for row in overlaps:
                self.stdout.write(
                    f"  · {row['resource']} {row['resource_id']}\n"
                    f"      course {row['trip_a']} ({row['destination_a']}) "
                    f"{row['window_a'][0]:%d/%m %H:%M} → {row['window_a'][1]:%H:%M}\n"
                    f"      course {row['trip_b']} ({row['destination_b']}) "
                    f"{row['window_b'][0]:%d/%m %H:%M} → {row['window_b'][1]:%H:%M}"
                )
        self.stderr.write(self.style.ERROR(
            f"{len(overlaps)} chevauchement(s) détecté(s). Corrigez-les (réaffecter, "
            "replanifier ou annuler l'une des deux courses) avant de migrer."
        ))
        # Sortie non nulle : un script de déploiement doit s'arrêter là.
        raise SystemExit(1)
