"""Admin Django Shield : consultation (les données viennent de Shield ; les correspondances et
résolutions passent par l'API d'administration, qui les journalise)."""
from django.contrib import admin

from apps.shield.models import ShieldCompany, ShieldDepartment, ShieldEmployee, ShieldSyncRun


class _ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False  # jamais de suppression : historique RH et liens de comptes

    def has_change_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return bool(request.user.is_active and request.user.is_staff)


@admin.register(ShieldSyncRun)
class ShieldSyncRunAdmin(_ReadOnlyAdmin):
    list_display = ("id", "mode", "status", "started_at", "finished_at", "resumed_count")
    list_filter = ("mode", "status")
    readonly_fields = [f.name for f in ShieldSyncRun._meta.fields]


@admin.register(ShieldCompany)
class ShieldCompanyAdmin(_ReadOnlyAdmin):
    list_display = ("shield_id", "code", "name", "is_active", "subsidiary", "mapping_confirmed_at")
    list_filter = ("is_active",)
    search_fields = ("code", "name")
    readonly_fields = [f.name for f in ShieldCompany._meta.fields]


@admin.register(ShieldDepartment)
class ShieldDepartmentAdmin(_ReadOnlyAdmin):
    list_display = ("shield_id", "code", "name", "company", "department")
    search_fields = ("code", "name")
    readonly_fields = [f.name for f in ShieldDepartment._meta.fields]


@admin.register(ShieldEmployee)
class ShieldEmployeeAdmin(_ReadOnlyAdmin):
    list_display = ("shield_id", "email", "last_name", "first_name", "status", "company", "user",
                    "conflict", "absent_since", "synced_at")
    list_filter = ("status", "conflict")
    search_fields = ("email", "last_name", "first_name", "matricule")
    readonly_fields = [f.name for f in ShieldEmployee._meta.fields]
    list_select_related = ("company", "user")
