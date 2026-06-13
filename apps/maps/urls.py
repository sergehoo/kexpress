from django.urls import path

from apps.maps.views import (
    NearbyVehiclesView,
    PlacesReverseView,
    PlacesSearchView,
    RouteEstimateView,
)

urlpatterns = [
    path("places/search/", PlacesSearchView.as_view(), name="places-search"),
    path("places/reverse/", PlacesReverseView.as_view(), name="places-reverse"),
    path("routes/estimate/", RouteEstimateView.as_view(), name="routes-estimate"),
    path("map/nearby-vehicles/", NearbyVehiclesView.as_view(), name="nearby-vehicles"),
]
