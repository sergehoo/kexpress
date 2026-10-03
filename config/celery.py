"""Application Celery — tâches asynchrones et périodiques (beat)."""
import os

from celery import Celery
from celery.schedules import crontab

# Défaut développement : en production, l'image Docker fixe déjà
# DJANGO_SETTINGS_MODULE=config.settings.production (setdefault ne l'écrase pas).
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

app = Celery("kexpress")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

app.conf.beat_schedule = {
    # Expirations (assurance, visite technique, permis) + maintenances dues : 2× / jour
    "check-expirations": {
        "task": "apps.notifications.tasks.check_expirations",
        "schedule": crontab(hour="7,15", minute=0),
    },
    # Courses en retard : toutes les 5 minutes
    "check-late-trips": {
        "task": "apps.notifications.tasks.check_late_trips",
        "schedule": 300.0,
    },
    # Fuel Intelligence : recalibrage du modèle de consommation (toutes les 6 h)
    "recalibrate-fuel-model": {
        "task": "apps.fuelintel.tasks.recalibrate_fuel_model",
        "schedule": crontab(hour="*/6", minute=15),
    },
    # Prix carburant CI : vérification quotidienne (fréquence configurable ici)
    "update-fuel-prices": {
        "task": "apps.fuelintel.tasks.update_fuel_prices",
        "schedule": crontab(hour=6, minute=0),
    },
    # Conformité véhicules : assurance / visite technique / révision (paliers)
    "check-vehicle-compliance": {
        "task": "apps.vehicles.tasks.check_vehicle_compliance",
        "schedule": crontab(hour="6,14", minute=30),
    },
    # Alertes critiques : poussées aux gestionnaires plutôt qu'attendues sur une page.
    # Toutes les 30 min : un « retour sans véhicule » doit être connu avant l'heure du départ.
    "push-critical-alerts": {
        "task": "apps.analytics.tasks.push_critical_alerts",
        "schedule": crontab(minute="*/30"),
    },
    # Coût kilométrique estimé des courses planifiées : rattrapage de ce qui échappe aux
    # points d'accroche (itinéraire calculé ailleurs, replanification). Idempotent.
    "refresh-trip-pricing": {
        "task": "apps.finance.tasks.refresh_trip_pricing",
        "schedule": crontab(minute="*/15"),
    },
    # F3 : seuils budgétaires franchis (une notification par seuil et par ligne).
    "check-budget-alerts": {
        "task": "apps.finance.tasks.check_budget_alerts",
        "schedule": crontab(minute="*/30"),
    },
    # Occupation & kilométrage à vide : matérialisation nocturne des 2 derniers jours
    # (la veille peut encore recevoir des clôtures tardives). La période « aujourd'hui »
    # reste calculée à la volée par l'API — cf. décision D4 (hybride batch + live).
    "recompute-occupancy-metrics": {
        "task": "apps.analytics.tasks.recompute_metrics",
        "schedule": crontab(hour=2, minute=20),
        "kwargs": {"days_back": 2},
    },
    # Kaydan Shield (référentiel RH) : employés modifiés toutes les 15 min (tri -updated_at +
    # borne, faute de filtre « modifié depuis » ou de webhook côté Shield)…
    "shield-incremental-sync": {
        "task": "apps.shield.tasks.shield_incremental_sync",
        "schedule": crontab(minute="*/15"),
    },
    # … et réconciliation complète nocturne (fiches absentes de Shield, fraîcheur < 26 h).
    "shield-full-reconcile": {
        "task": "apps.shield.tasks.shield_full_reconcile",
        "schedule": crontab(hour=1, minute=40),
    },
    # Car Plan : échéances, retards de restitution, quotas, relevés, remises, validations
    # (anti-doublon par l'historique de l'attribution) — 2× / jour.
    "check-car-plan": {
        "task": "apps.carplan.tasks.check_car_plan",
        "schedule": crontab(hour="7,15", minute=10),
    },
    # Plans d'entretien prédictifs : prévisions recalculées, alertes selon l'état mémorisé de
    # chaque plan (préavis, alerte, urgence, dépassement, rythme en hausse) — 2× / jour.
    "check-maintenance-plans": {
        "task": "apps.maintenance.tasks.check_maintenance_plans",
        "schedule": crontab(hour="7,15", minute=20),
    },
}
