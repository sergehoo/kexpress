from django.urls import path
from rest_framework.routers import DefaultRouter

from apps.notifications.views import (
    EmailLogViewSet,
    NotificationPreferencesView,
    NotificationViewSet,
    PushSubscribeView,
    VapidKeyView,
)

router = DefaultRouter()
router.register("notifications", NotificationViewSet, basename="notification")
router.register("notification-emails", EmailLogViewSet, basename="notification-email")

urlpatterns = [
    path("push/vapid-key/", VapidKeyView.as_view(), name="push-vapid-key"),
    path("push/subscribe/", PushSubscribeView.as_view(), name="push-subscribe"),
    path("notification-preferences/", NotificationPreferencesView.as_view(), name="notification-preferences"),
] + router.urls
