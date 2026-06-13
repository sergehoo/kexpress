from rest_framework import serializers

from apps.audit.models import AuditLog


class AuditLogSerializer(serializers.ModelSerializer):
    action_display = serializers.CharField(source="get_action_display", read_only=True)
    actor_email = serializers.CharField(source="actor.email", read_only=True, default=None)
    actor_name = serializers.CharField(source="actor.get_full_name", read_only=True, default=None)

    class Meta:
        model = AuditLog
        fields = [
            "id", "created_at", "action", "action_display", "actor", "actor_email",
            "actor_name", "target_repr", "changes", "ip_address",
        ]
