from django.apps import AppConfig


class CarPlanConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.carplan"
    label = "carplan"
    verbose_name = "Car Plan (véhicules de fonction et de service attribués)"

    def ready(self):
        from apps.carplan import signals  # noqa: F401 — historique et verrous
