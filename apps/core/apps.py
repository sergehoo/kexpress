from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
    verbose_name = "Noyau"

    def ready(self):
        # Fichiers téléversés : URL signée à durée limitée dans TOUTES les API, jamais le
        # chemin `/media/` (qui n'est plus servi).
        from apps.core.secure_files import install_on_model_serializers

        install_on_model_serializers()
