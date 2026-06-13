from django.urls import re_path

from apps.tracking.consumers import FleetConsumer, TripTrackingConsumer

websocket_urlpatterns = [
    re_path(r"^ws/fleet/$", FleetConsumer.as_asgi()),
    re_path(r"^ws/trips/(?P<trip_id>[0-9a-f-]+)/tracking/$", TripTrackingConsumer.as_asgi()),
]
