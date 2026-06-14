from django.urls import path

from apps.maps.views import (
    DriversNearestView,
    NearbyVehiclesView,
    PlacesReverseView,
    PlacesSearchView,
    RouteCalculateView,
    RouteEstimateView,
)

urlpatterns = [
    path("places/search/", PlacesSearchView.as_view(), name="places-search"),
    # Alias attendu par la spec : autocomplétion = recherche de lieux.
    path("places/autocomplete/", PlacesSearchView.as_view(), name="places-autocomplete"),
    path("places/reverse/", PlacesReverseView.as_view(), name="places-reverse"),
    path("routes/estimate/", RouteEstimateView.as_view(), name="routes-estimate"),
    path("routes/calculate/", RouteCalculateView.as_view(), name="routes-calculate"),
    path("map/nearby-vehicles/", NearbyVehiclesView.as_view(), name="nearby-vehicles"),
    # Affectation au plus proche (ETA OSRM). Sous le préfixe map/ pour éviter la
    # collision avec les routers DRF vehicles/<pk>/ et drivers/<pk>/.
    path("map/vehicles/nearest/", NearbyVehiclesView.as_view(), name="vehicles-nearest"),
    path("map/drivers/nearest/", DriversNearestView.as_view(), name="drivers-nearest"),
]
