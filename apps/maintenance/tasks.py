"""Tâche périodique des plans d'entretien prédictifs."""
from celery import shared_task


@shared_task
def check_maintenance_plans() -> dict:
    from apps.maintenance.predictive import check_plans

    return check_plans()
