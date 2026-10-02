"""Révoque l'ancien mot de passe par défaut partout où il est encore actif.

L'API créait les comptes sans mot de passe avec un mot de passe commun et connu. Le supprimer
du code ne suffit pas : les comptes déjà créés le portent encore. Ils reçoivent un mot de passe
inutilisable — leurs titulaires le redéfinissent par une invitation (`/api/employees/<id>/invite/`).
"""
from django.db import migrations

_FORMER_DEFAULT = "demo" + "1234"  # valeur historique, à révoquer (jamais réutilisée)


def revoke(apps, schema_editor):
    from django.contrib.auth.hashers import check_password, make_password

    User = apps.get_model("accounts", "User")
    for user in User.objects.exclude(password="").exclude(password__startswith="!").only("pk", "password"):
        if check_password(_FORMER_DEFAULT, user.password):
            User.objects.filter(pk=user.pk).update(password=make_password(None))


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0004_user_keycloak_id_user_keycloak_sync_error_and_more"),
    ]

    operations = [migrations.RunPython(revoke, migrations.RunPython.noop)]
