from django.contrib import admin

from apps.notifications.models import Notification, NotificationPreference


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ["title", "recipient", "notification_type", "channel", "severity", "is_read", "created_at"]
    list_filter = ["notification_type", "channel", "severity", "is_read"]
    search_fields = ["title", "recipient__email"]


@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(admin.ModelAdmin):
    list_display = ["user", "notification_type", "in_app", "email", "sms", "whatsapp", "push"]
    list_filter = ["notification_type"]


from apps.notifications.models import EmailLog, EmailTemplate


@admin.register(EmailTemplate)
class EmailTemplateAdmin(admin.ModelAdmin):
    list_display = ["key", "subject", "is_active", "updated_at"]
    list_filter = ["is_active"]
    search_fields = ["key", "subject"]


@admin.register(EmailLog)
class EmailLogAdmin(admin.ModelAdmin):
    list_display = ["subject", "to_email", "status", "created_at"]
    list_filter = ["status"]
    search_fields = ["to_email", "subject"]
    readonly_fields = [f.name for f in EmailLog._meta.fields]
