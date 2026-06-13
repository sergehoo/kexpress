from django.conf import settings
from django.utils import timezone
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.notifications.models import Notification, PushSubscription
from apps.notifications.serializers import NotificationSerializer


class VapidKeyView(APIView):
    """Clé publique VAPID pour l'abonnement Web Push côté navigateur."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"public_key": settings.VAPID_PUBLIC_KEY})


class PushSubscribeView(APIView):
    """Enregistre / supprime l'abonnement push du navigateur courant."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        endpoint = request.data.get("endpoint")
        keys = request.data.get("keys") or {}
        if not (endpoint and keys.get("p256dh") and keys.get("auth")):
            return Response({"detail": "Abonnement push invalide."}, status=400)
        PushSubscription.objects.update_or_create(
            endpoint=endpoint,
            defaults={
                "user": request.user,
                "p256dh": keys["p256dh"],
                "auth": keys["auth"],
                "user_agent": request.META.get("HTTP_USER_AGENT", "")[:255],
            },
        )
        return Response({"status": "subscribed"})

    def delete(self, request):
        endpoint = request.data.get("endpoint")
        deleted, _ = PushSubscription.objects.filter(
            user=request.user, endpoint=endpoint or ""
        ).delete()
        return Response({"status": "unsubscribed", "deleted": deleted})


class NotificationViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """Notifications de l'utilisateur courant."""

    serializer_class = NotificationSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["is_read", "severity"]

    def get_queryset(self):
        return Notification.objects.filter(recipient=self.request.user)

    @action(detail=False, methods=["get"])
    def unread_count(self, request):
        count = self.get_queryset().filter(is_read=False).count()
        return Response({"count": count})

    @action(detail=False, methods=["post"])
    def mark_all_read(self, request):
        self.get_queryset().filter(is_read=False).update(
            is_read=True, read_at=timezone.now()
        )
        return Response({"status": "ok"})

    @action(detail=True, methods=["post"])
    def mark_read(self, request, pk=None):
        notif = self.get_queryset().filter(pk=pk).first()
        if notif and not notif.is_read:
            notif.is_read = True
            notif.read_at = timezone.now()
            notif.save(update_fields=["is_read", "read_at"])
        return Response({"status": "ok"})


class EmailLogViewSet(viewsets.ReadOnlyModelViewSet):
    """Historique des emails (administrateurs) + relance manuelle."""

    permission_classes = [IsAuthenticated]
    filterset_fields = ["status"]
    search_fields = ["to_email", "subject"]

    def get_serializer_class(self):
        from rest_framework import serializers as drf

        from apps.notifications.models import EmailLog

        class _Ser(drf.ModelSerializer):
            status_display = drf.CharField(source="get_status_display", read_only=True)
            recipient_name = drf.CharField(source="recipient.get_full_name", read_only=True)
            notification_type = drf.CharField(
                source="notification.notification_type", read_only=True, default=None
            )

            class Meta:
                model = EmailLog
                fields = [
                    "id", "to_email", "recipient_name", "subject", "status",
                    "status_display", "error", "notification_type", "created_at",
                ]

        return _Ser

    def get_queryset(self):
        from apps.core.enums import COMPANY_SCOPE_ROLES, RoleChoices
        from apps.notifications.models import EmailLog

        u = self.request.user
        qs = EmailLog.objects.select_related("recipient", "notification")
        if u.is_superuser or u.role in COMPANY_SCOPE_ROLES:
            return qs
        if u.role == RoleChoices.SUBSIDIARY_ADMIN and u.subsidiary_id:
            return qs.filter(recipient__subsidiary_id=u.subsidiary_id)
        return qs.filter(recipient=u)

    @action(detail=True, methods=["post"])
    def resend(self, request, pk=None):
        """Relance manuelle : renvoie l'email de la notification d'origine."""
        from apps.notifications.services import send_email_for

        log = self.get_object()
        if not log.notification:
            return Response({"detail": "Notification d'origine introuvable."}, status=400)
        new_log = send_email_for(log.notification, force=True)
        return Response({
            "detail": "Relance effectuée." if new_log and new_log.status == "sent"
                      else f"Relance : {new_log.get_status_display() if new_log else 'impossible'}.",
            "status": new_log.status if new_log else "failed",
        })


class NotificationPreferencesView(APIView):
    """Préférences de canaux par type pour l'utilisateur courant (GET/PUT)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.core.enums import NotificationType
        from apps.notifications.models import NotificationPreference

        prefs = {
            p.notification_type: p
            for p in NotificationPreference.objects.filter(user=request.user)
        }
        return Response({
            "results": [
                {
                    "notification_type": value,
                    "label": label,
                    "in_app": prefs[value].in_app if value in prefs else True,
                    "email": prefs[value].email if value in prefs else True,
                    "push": prefs[value].push if value in prefs else True,
                }
                for value, label in NotificationType.choices
            ]
        })

    def put(self, request):
        from apps.core.enums import NotificationType
        from apps.notifications.models import NotificationPreference

        valid = dict(NotificationType.choices)
        updated = 0
        for row in request.data.get("results", []):
            ntype = row.get("notification_type")
            if ntype not in valid:
                continue
            NotificationPreference.objects.update_or_create(
                user=request.user, notification_type=ntype,
                defaults={
                    "in_app": bool(row.get("in_app", True)),
                    "email": bool(row.get("email", True)),
                    "push": bool(row.get("push", True)),
                },
            )
            updated += 1
        return Response({"detail": f"{updated} préférence(s) enregistrée(s)."})
