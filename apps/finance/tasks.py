"""Tâches planifiées Finance."""
from __future__ import annotations

from datetime import timedelta

from celery import shared_task
from django.utils import timezone


@shared_task
def refresh_trip_pricing(days_back: int = 1, days_ahead: int = 60) -> dict:
    """Rattrape l'estimation du coût kilométrique des courses planifiées.

    Les points d'accroche (création de la course, calcul de l'itinéraire, changement de
    barème) couvrent le cas courant ; cette passe rattrape ce qui leur échappe — un itinéraire
    calculé par un autre chemin, une course replanifiée hors du flux habituel. Idempotente :
    une course inchangée n'est pas réécrite, une course figée n'est jamais touchée.
    """
    from apps.finance.trip_cost import freeze_direct_backlog
    from apps.finance.trip_pricing import freeze_closed_backlog, refresh_open_trips

    today = timezone.localdate()
    updated = refresh_open_trips(since=today - timedelta(days=days_back),
                                 until=today + timedelta(days=days_ahead))
    # Un gel raté à la clôture laisserait la course non figée à vie : on le rejoue.
    frozen = freeze_closed_backlog()
    # Idem pour le coût réel direct (courses clôturées avant F1 ou dont le gel a échoué) —
    # APRÈS le barème, dont il reprend la distance mesurée.
    costed = freeze_direct_backlog()
    return {"trips_updated": updated, "closed_trips_frozen": frozen, "closed_trips_costed": costed}


@shared_task
def check_budget_alerts() -> dict:
    """Notifie les seuils de consommation franchis par les budgets approuvés de l'année."""
    from apps.finance.budget import check_alerts

    return {"alerts": check_alerts()}
