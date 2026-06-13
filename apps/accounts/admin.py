from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from apps.accounts.models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ["email"]
    list_display = ["email", "get_full_name", "role", "subsidiary", "is_active", "is_staff"]
    list_filter = ["role", "is_active", "is_staff", "subsidiary"]
    search_fields = ["email", "first_name", "last_name", "phone"]
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Identité", {"fields": ("first_name", "last_name", "phone")}),
        ("Organisation", {"fields": ("role", "subsidiary", "department", "manager")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "password1", "password2", "role", "subsidiary"),
        }),
    )
    readonly_fields = ["date_joined", "last_login"]
