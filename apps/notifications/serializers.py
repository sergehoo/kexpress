from rest_framework import serializers

from apps.notifications.models import Notification


class NotificationSerializer(serializers.ModelSerializer):
    type_display = serializers.CharField(source="get_notification_type_display", read_only=True)

    def to_representation(self, instance):
        """Montants masqués pour un lecteur qui n'a plus le droit de lire les dépenses."""
        from apps.notifications.visibility import redact

        data = super().to_representation(instance)
        request = self.context.get("request")
        if request is not None:
            data["title"] = redact(data.get("title") or "", request.user)
            data["message"] = redact(data.get("message") or "", request.user)
        return data

    class Meta:
        model = Notification
        fields = [
            "id", "notification_type", "type_display", "channel", "severity",
            "title", "message", "link", "is_read", "read_at", "created_at",
        ]
        read_only_fields = fields
