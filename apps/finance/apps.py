from django.apps import AppConfig


class FinanceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.finance"
    verbose_name = "finance & coûts"

    def ready(self):
        # Verrous de l'historique financier (course clôturée, mois clos) sur toutes les
        # sources de coût : API, admin, ORM et suppressions en cascade.
        from apps.finance import locks

        locks.connect()
