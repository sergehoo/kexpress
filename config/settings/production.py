"""Réglages de production — Kaydan Express.

Toutes les valeurs sensibles proviennent de l'environnement. Aucune valeur de
développement n'est utilisée ici : SECRET_KEY et ALLOWED_HOSTS sont obligatoires.
Conçu pour tourner derrière un reverse proxy terminant TLS (Traefik / Dokploy).
"""
from .base import *  # noqa: F401,F403
from .base import NOTIFY_EMAIL_ENABLED, env

# --- Sécurité de base -----------------------------------------------------
DEBUG = False

# Obligatoires en production (lèvent une erreur explicite si absents).
SECRET_KEY = env("SECRET_KEY")
# ALLOWED_HOSTS depuis l'environnement + loopback pour les health checks internes.
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=[]) + ["localhost", "127.0.0.1"]

# Origines de confiance CSRF (schéma inclus) : https://app.exemple.com,https://api.exemple.com
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

# --- Fichiers statiques : WhiteNoise --------------------------------------
# Insère le middleware WhiteNoise juste après le SecurityMiddleware.
_mw = list(MIDDLEWARE)  # noqa: F405
_security = "django.middleware.security.SecurityMiddleware"
_mw.insert(_mw.index(_security) + 1, "whitenoise.middleware.WhiteNoiseMiddleware")
MIDDLEWARE = _mw

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# --- Durcissement HTTP (derrière un proxy TLS) ----------------------------
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

# Redirection HTTPS : le proxy (Traefik) la gère en général ; activable au besoin.
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=False)
# HSTS (30 jours par défaut). Mettre à 0 le temps de valider le certificat.
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=2_592_000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", default=True)
SECURE_HSTS_PRELOAD = env.bool("SECURE_HSTS_PRELOAD", default=True)

# --- Base de données : connexions persistantes ----------------------------
DATABASES["default"]["CONN_MAX_AGE"] = env.int("CONN_MAX_AGE", default=60)  # noqa: F405
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True  # noqa: F405

# --- Email ----------------------------------------------------------------
# Si les notifications email sont activées, on bascule sur SMTP (sinon console).
if NOTIFY_EMAIL_ENABLED:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = env("EMAIL_HOST", default="")
    EMAIL_PORT = env.int("EMAIL_PORT", default=587)
    EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
    EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
    EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
    EMAIL_USE_SSL = env.bool("EMAIL_USE_SSL", default=False)
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# --- Journalisation (stdout, capté par Docker / Dokploy) ------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "[{asctime}] {levelname} {name}: {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", default="INFO")},
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "daphne": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "celery": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
