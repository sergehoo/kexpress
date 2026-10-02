from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounts"
    verbose_name = "Comptes utilisateurs"

    def ready(self):
        from apps.accounts import checks  # noqa: F401 — enregistre les contrôles de déploiement
