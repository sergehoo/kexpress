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
}
