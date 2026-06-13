import os

from django.core.asgi import get_asgi_application

# Défaut développement : en production, l'image Docker fixe déjà
# DJANGO_SETTINGS_MODULE=config.settings.production (setdefault ne l'écrase pas).
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

# L'application HTTP doit être initialisée avant d'importer le code qui touche aux modèles.
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402

from apps.tracking.routing import websocket_urlpatterns  # noqa: E402
from apps.tracking.ws_auth import JWTAuthMiddleware  # noqa: E402

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": JWTAuthMiddleware(URLRouter(websocket_urlpatterns)),
    }
)
