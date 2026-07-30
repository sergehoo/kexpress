from rest_framework.routers import DefaultRouter

from django.urls import path

from apps.dispatch.views import (
    DispatchBoardView,
    DispatchSuggestionViewSet,
    MissionViewSet,
)

router = DefaultRouter()
router.register("missions", MissionViewSet, basename="mission")
router.register("dispatch-suggestions", DispatchSuggestionViewSet, basename="dispatch-suggestion")

urlpatterns = router.urls + [
    path("dispatch/board/", DispatchBoardView.as_view(), name="dispatch-board"),
]
