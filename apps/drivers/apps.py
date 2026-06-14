from django.apps import AppConfig


class DriversConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.drivers"
    verbose_name = "Chauffeurs"

    def ready(self):
        # Branche le provisioning automatique du profil chauffeur (signal post_save User).
        from apps.drivers import signals  # noqa: F401
