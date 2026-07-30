from django.apps import AppConfig


class FuelintelConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.fuelintel"
    # Libellé visible : le module couvre toutes les énergies (carburants ET électricité),
    # pas seulement le carburant. Le nom technique reste `fuelintel` (cf. models.py).
    verbose_name = "Gestion de l'énergie"
