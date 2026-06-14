from django.urls import path

from apps.tracking.views import (
    FleetPositionsView,
    GeofenceZonesView,
    LiveAlertsView,
    TripPositionIngestView,
    TripReplayView,
    TripRouteView,
)

urlpatterns = [
    path("tracking/positions/", FleetPositionsView.as_view(), name="fleet-positions"),
    path("tracking/zones/", GeofenceZonesView.as_view(), name="geofence-zones"),
    path("tracking/live-alerts/", LiveAlertsView.as_view(), name="live-alerts"),
    path("tracking/trips/<uuid:trip_id>/route/", TripRouteView.as_view(), name="trip-route"),
    path("tracking/trips/<uuid:trip_id>/position/", TripPositionIngestView.as_view(), name="trip-position"),
    path("tracking/trips/<uuid:trip_id>/replay/", TripReplayView.as_view(), name="trip-replay"),
]
