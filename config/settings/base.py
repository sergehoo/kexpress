"""
Réglages de base — Kaydan Express.

Découpé en base/local ; les secrets proviennent de variables d'environnement
(fichier .env via django-environ). Ne jamais committer de vrais secrets.
"""
from datetime import timedelta
from pathlib import Path

import environ

# BASE_DIR = racine du projet (dossier contenant manage.py)
BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    SECRET_KEY=(str, "dev-insecure-change-me"),
    ALLOWED_HOSTS=(list, ["*"]),
    CORS_ALLOWED_ORIGINS=(list, ["http://localhost:3000"]),
)

# Charge .env s'il existe
env_file = BASE_DIR / ".env"
if env_file.exists():
    environ.Env.read_env(str(env_file))

SECRET_KEY = env("SECRET_KEY")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

# --- Applications ---------------------------------------------------------
DJANGO_APPS = [
    "daphne",  # doit précéder staticfiles : intègre l'ASGI dans runserver
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.gis",  # GeoDjango (PostGIS) : géométries, géofencing, distances
]

THIRD_PARTY_APPS = [
    "channels",
    "rest_framework",
    "rest_framework_simplejwt",
    "corsheaders",
    "django_filters",
    "drf_spectacular",
]

LOCAL_APPS = [
    "apps.core",
    "apps.accounts",
    "apps.organizations",
    "apps.vehicles",
    "apps.drivers",
    "apps.reservations",
    "apps.trips",
    "apps.maintenance",
    "apps.expenses",
    "apps.tracking",
    "apps.notifications",
    "apps.audit",
    "apps.fuelintel",
    "apps.kbot",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# --- Channels (WebSocket temps réel) --------------------------------------
REDIS_URL = env("REDIS_URL", default="redis://127.0.0.1:6379/0")
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {"hosts": [REDIS_URL]},
    }
}

# --- Base de données (PostGIS) --------------------------------------------
DATABASES = {
    "default": env.db(
        "DATABASE_URL",
        default="postgres://sergeogah@127.0.0.1:5433/kexpress",
    )
}
# Moteur spatial GeoDjango (PointField/PolygonField, géofencing, distances).
DATABASES["default"]["ENGINE"] = "django.contrib.gis.db.backends.postgis"

# Bibliothèques natives GDAL/GEOS : auto-détectées sous Linux (Docker) ;
# chemins explicites possibles via env (utile sous macOS/Homebrew, cf. local.py).
GDAL_LIBRARY_PATH = env("GDAL_LIBRARY_PATH", default=None)
GEOS_LIBRARY_PATH = env("GEOS_LIBRARY_PATH", default=None)

# --- Auth -----------------------------------------------------------------
AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- Internationalisation -------------------------------------------------
LANGUAGE_CODE = "fr-fr"
TIME_ZONE = "Africa/Abidjan"
USE_I18N = True
USE_TZ = True

# --- Fichiers statiques / médias ------------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Keycloak / OIDC (SSO) -------------------------------------------------
# Keycloak authentifie ; Kexpress gère l'autorisation (rôle + filiale).
OIDC_ENABLED = env.bool("OIDC_ENABLED", default=False)
OIDC_ISSUER = env("OIDC_ISSUER", default="").rstrip("/")
OIDC_JWKS_URL = env(
    "OIDC_JWKS_URL",
    default=(f"{OIDC_ISSUER}/protocol/openid-connect/certs" if OIDC_ISSUER else ""),
)
OIDC_CLIENT_ID = env("OIDC_CLIENT_ID", default="kexpress-web")
# Auditoires acceptés (claim `aud`). Vide → on n'exige pas d'audience mais on
# vérifie que `azp`/`aud` correspond au client (cf. authentication.py).
OIDC_AUDIENCE = env.list("OIDC_AUDIENCE", default=[])
# Rôle attribué au 1er login SSO (un admin ajuste rôle + filiale ensuite).
OIDC_DEFAULT_ROLE = env("OIDC_DEFAULT_ROLE", default="requester")
# Connexion locale par mot de passe. Quand OIDC est actif, /api/auth/token/
# n'accepte plus que les super-admins (accès de secours « break-glass »).
LOCAL_LOGIN_ENABLED = env.bool("LOCAL_LOGIN_ENABLED", default=True)

# --- Django REST Framework ------------------------------------------------
_AUTH_CLASSES = []
if OIDC_ENABLED:
    _AUTH_CLASSES.append("apps.accounts.authentication.KeycloakAuthentication")
_AUTH_CLASSES.append("rest_framework_simplejwt.authentication.JWTAuthentication")
if not OIDC_ENABLED:
    # SessionAuthentication (cookie + CSRF) uniquement hors SSO : évite une voie
    # d'authentification non-OIDC lorsque le SSO est obligatoire. (Le site /admin
    # Django reste fonctionnel : il n'en dépend pas.)
    _AUTH_CLASSES.append("rest_framework.authentication.SessionAuthentication")

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": tuple(_AUTH_CLASSES),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_FILTER_BACKENDS": (
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ),
    "DEFAULT_PAGINATION_CLASS": "apps.core.pagination.DefaultPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=60),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": False,
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Kaydan Express API",
    "DESCRIPTION": "Plateforme de gestion de flotte multi-filiales.",
    "VERSION": "0.1.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

# --- CORS -----------------------------------------------------------------
CORS_ALLOWED_ORIGINS = env("CORS_ALLOWED_ORIGINS")

# --- Web Push (VAPID) ------------------------------------------------------
VAPID_PUBLIC_KEY = env("VAPID_PUBLIC_KEY", default="")
VAPID_PRIVATE_KEY = env("VAPID_PRIVATE_KEY", default="")
VAPID_ADMIN_EMAIL = env("VAPID_ADMIN_EMAIL", default="admin@example.com")

# --- Celery (tâches asynchrones + périodiques) ----------------------------
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_TASK_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE

# --- Fuel Intelligence ------------------------------------------------------
# Prix carburant CI (réglementés) — repli si la source distante est absente/injoignable.
FUEL_PRICE_SOURCE_URL = env("FUEL_PRICE_SOURCE_URL", default="")
FUEL_PRICE_SUPER = env("FUEL_PRICE_SUPER", default="875")
FUEL_PRICE_GASOIL = env("FUEL_PRICE_GASOIL", default="655")

# --- K-BOT (assistant IA) -------------------------------------------------
# Si une clé API est fournie, K-BOT utilise un LLM pour les questions libres, avec
# un contexte RAG construit à partir des données AUTORISÉES de l'utilisateur. Sinon,
# le moteur heuristique ancré sur les données prend le relais.
#
# Fournisseur par défaut : DeepSeek (API compatible OpenAI, économique). Anthropic
# (Claude) reste disponible via KBOT_PROVIDER=anthropic. Le fournisseur est déduit
# automatiquement de la clé présente si KBOT_PROVIDER n'est pas fixé.
DEEPSEEK_API_KEY = env("DEEPSEEK_API_KEY", default="")
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", default="")

KBOT_PROVIDER = env("KBOT_PROVIDER", default="").lower()
if not KBOT_PROVIDER:
    # Auto : DeepSeek si sa clé est présente (ou si aucune clé Claude), sinon Anthropic.
    KBOT_PROVIDER = "anthropic" if (ANTHROPIC_API_KEY and not DEEPSEEK_API_KEY) else "deepseek"

# Clé active : KBOT_API_KEY générique sinon la clé spécifique au fournisseur retenu.
KBOT_API_KEY = env("KBOT_API_KEY", default="") or (
    DEEPSEEK_API_KEY if KBOT_PROVIDER == "deepseek" else ANTHROPIC_API_KEY
)

# Modèle par défaut selon le fournisseur (surchargeable via KBOT_MODEL).
_KBOT_DEFAULT_MODEL = {"deepseek": "deepseek-chat", "anthropic": "claude-3-5-sonnet-latest"}
KBOT_MODEL = env("KBOT_MODEL", default=_KBOT_DEFAULT_MODEL.get(KBOT_PROVIDER, "deepseek-chat"))
# Endpoint compatible OpenAI (DeepSeek par défaut ; ignoré par le fournisseur Anthropic).
KBOT_BASE_URL = env("KBOT_BASE_URL", default="https://api.deepseek.com")
KBOT_MAX_TOKENS = env.int("KBOT_MAX_TOKENS", default=600)

# --- Carte / réservation ---------------------------------------------------
# Services de routage/géocodage : publics par défaut, auto-hébergeables en prod
# (conteneurs dédiés) via OSRM_URL / NOMINATIM_URL.
OSRM_URL = env("OSRM_URL", default="https://router.project-osrm.org")
NOMINATIM_URL = env("NOMINATIM_URL", default="https://nominatim.openstreetmap.org")
MAP_COST_PER_KM = env.float("MAP_COST_PER_KM", default=350.0)

# Notifications email (canal optionnel ; backend console en local, SMTP via EMAIL_* env)
NOTIFY_EMAIL_ENABLED = env.bool("NOTIFY_EMAIL_ENABLED", default=False)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="noreply@kaydan-express.ci")

# Seuils de rappel révision en %% de l'intervalle du véhicule (admin/env)
REVISION_ALERT_PCTS = env("REVISION_ALERT_PCTS", default="20,10,5")
