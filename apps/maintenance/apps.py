from django.apps import AppConfig


class MaintenanceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.maintenance"
    verbose_name = "Maintenance"

    def ready(self):
        from apps.maintenance import signals  # noqa: F401 — plan d'entretien à la clôture d'une intervention
