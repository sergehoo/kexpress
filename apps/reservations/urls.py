from django.urls import path
from rest_framework.routers import DefaultRouter

from apps.reservations.views import ReservationFromMapView, ReservationViewSet

router = DefaultRouter()
router.register("reservations", ReservationViewSet, basename="reservation")

# L'URL explicite doit précéder le routeur (sinon « from-map » serait pris pour un pk).
urlpatterns = [
    path("reservations/from-map/", ReservationFromMapView.as_view(), name="reservation-from-map"),
] + router.urls
