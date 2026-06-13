from django.urls import path
from rest_framework.routers import DefaultRouter

from apps.trips.incidents_view import IncidentsView
from apps.trips.views import TripViewSet

router = DefaultRouter()
router.register("trips", TripViewSet, basename="trip")

urlpatterns = router.urls + [
    path("incidents/", IncidentsView.as_view(), name="incidents"),
]
