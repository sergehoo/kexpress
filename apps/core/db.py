"""Utilitaires SQL partagés (expressions PostgreSQL réutilisables)."""
from __future__ import annotations

from django.contrib.postgres.fields import DateTimeRangeField
from django.db.models import Func


class TsTzRange(Func):
    """`TSTZRANGE(debut, fin, '[)')` — fenêtre temporelle demi-ouverte.

    Demi-ouverte volontairement : une course qui finit à 12:00 et une qui part à 12:00
    ne se chevauchent PAS (le véhicule est libéré à l'instant d'arrivée). Utilisée par les
    contraintes d'exclusion anti-double-booking (cf. `apps.trips.models.Trip.Meta`).
    """

    function = "TSTZRANGE"
    output_field = DateTimeRangeField()


def lock_row(obj):
    """Re-lit la ligne AVEC verrou exclusif (`SELECT … FOR UPDATE`) dans la transaction courante.

    Sans ce verrou, deux affectations concurrentes du MÊME véhicule (ou chauffeur) lisent
    toutes les deux « aucun conflit » avant que l'une ait committé (write-skew sous
    READ COMMITTED, l'isolement par défaut de PostgreSQL) et créent un double-booking :
    `@transaction.atomic` garantit l'atomicité, PAS l'isolement. Avec le verrou, la seconde
    transaction attend, puis revoit le conflit fraîchement committé et le refuse proprement
    (message métier) au lieu de heurter la contrainte d'exclusion.

    À appeler à l'intérieur d'un `@transaction.atomic`.
    """
    return type(obj)._base_manager.select_for_update().get(pk=obj.pk)
