"""Réglages de développement local."""
import os
import platform

from .base import *  # noqa: F401,F403
from .base import GDAL_LIBRARY_PATH, GEOS_LIBRARY_PATH, env

DEBUG = env.bool("DEBUG", default=True)

INSTALLED_APPS += ["django_extensions"]  # noqa: F405

# Email en console pour le dev (notifications email simulées)
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# --- GeoDjango sous macOS (Homebrew) --------------------------------------
# L'auto-détection des libs GDAL/GEOS échoue souvent avec Homebrew (noms
# versionnés). On pointe explicitement les dylib si aucune valeur d'env n'est
# fournie et qu'elles existent. Sans effet sous Linux.
if platform.system() == "Darwin":
    _hb = os.environ.get("HOMEBREW_PREFIX", "/opt/homebrew") + "/lib"
    if not GDAL_LIBRARY_PATH and os.path.exists(f"{_hb}/libgdal.dylib"):
        GDAL_LIBRARY_PATH = f"{_hb}/libgdal.dylib"
    if not GEOS_LIBRARY_PATH and os.path.exists(f"{_hb}/libgeos_c.dylib"):
        GEOS_LIBRARY_PATH = f"{_hb}/libgeos_c.dylib"
