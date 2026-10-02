"""Tâches périodiques Car Plan : échéances, retards, quotas, relevés, remises, validations."""
from celery import shared_task


@shared_task
def check_car_plan() -> dict:
    from apps.carplan.operations import activate_planned_replacements, check_assignments

    result = check_assignments()
    result["replacements_started"] = activate_planned_replacements()
    return result
