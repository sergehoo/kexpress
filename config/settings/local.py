"""Réglages de développement local."""
from .base import *  # noqa: F401,F403
from .base import env

DEBUG = env.bool("DEBUG", default=True)

INSTALLED_APPS += ["django_extensions"]  # noqa: F405

# Email en console pour le dev (notifications email simulées)
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
