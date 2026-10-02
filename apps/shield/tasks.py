"""Tâches planifiées Kaydan Shield (cf. `config/celery.py`)."""
from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings

logger = logging.getLogger("apps.shield.tasks")


def _summary(run) -> dict:
    if run is None:
        return {"status": "skipped", "detail": "Synchronisation déjà en cours."}
    return {"status": run.status, "run": run.pk, "mode": run.mode, "counters": run.counters,
            "error": run.error}


@shared_task
def shield_run(mode: str, triggered_by_id=None, force: bool = False) -> dict:
    """Exécute (ou reprend) une synchronisation Shield du mode demandé."""
    from apps.shield.sync import run_sync

    if not settings.SHIELD_ENABLED:
        return {"status": "disabled"}
    actor = None
    if triggered_by_id:
        from apps.accounts.models import User

        actor = User.objects.filter(pk=triggered_by_id).first()
    return _summary(run_sync(mode, triggered_by=actor, force=force))


@shared_task
def shield_incremental_sync() -> dict:
    """Toutes les 15 minutes : employés modifiés depuis la dernière borne — ou RATTRAPAGE de la
    réconciliation nocturne si elle a été manquée (exécution déjà en cours à 01:40, échec,
    interruption) : sans elle, les fiches non modifiées vieillissent et plus aucune activation
    n'est possible au-delà de SHIELD_MAX_STALENESS_HOURS."""
    from apps.shield.sync import reconcile_due

    if not settings.SHIELD_ENABLED:
        return {"status": "disabled"}
    return shield_run("reconcile" if reconcile_due() else "incremental")


@shared_task
def shield_full_reconcile() -> dict:
    """Chaque nuit : lecture complète + constat des fiches absentes de Shield."""
    return shield_run("reconcile")
