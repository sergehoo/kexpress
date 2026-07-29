"""Garde-fou de déploiement : les contrôles système Django doivent passer AVEC la base.

Pourquoi ce test existe : `manage.py check` **seul n'exécute pas** les contrôles dépendants
de la base de données. `manage.py migrate` les exécute, lui — un projet peut donc passer tous
ses tests et son `check` en local, puis refuser de démarrer en production.

C'est arrivé : la contrainte d'exclusion anti-double-booking de `Trip` exigeait
`django.contrib.postgres` dans `INSTALLED_APPS` ; `check` était vert, et le conteneur
bouclait au démarrage sur `postgres.E005`, ce qui le rendait *unhealthy* et le retirait
du routage — API et WebSocket en 404.
"""
import pytest
from django.core.management import call_command


@pytest.mark.django_db
def test_system_checks_pass_with_database():
    """Équivalent de `manage.py check --database default`, ce que fait `migrate` au boot.

    `call_command("check")` lève `SystemCheckError` en cas d'erreur : la seule absence
    d'exception est la preuve attendue.
    """
    call_command("check", "--database", "default", verbosity=0)
