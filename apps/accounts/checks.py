"""Contrôles de déploiement des invitations (`manage.py check --deploy`)."""
from django.conf import settings
from django.core.checks import Warning, register


@register("security", deploy=True)
def invitation_delivery(app_configs, **kwargs):
    from apps.accounts.invitations import delivery_problems

    if getattr(settings, "OIDC_ENABLED", False):
        return []  # le SSO envoie lui-même les emails d'activation
    return [Warning(problem, hint="Définissez FRONTEND_URL (adresse publique) et un envoi SMTP.",
                    id="accounts.W001") for problem in delivery_problems()]
