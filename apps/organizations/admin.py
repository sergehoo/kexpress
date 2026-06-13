from django.contrib import admin

from apps.organizations.models import Company, Department, Subsidiary


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ["name", "legal_name", "is_active"]
    search_fields = ["name", "legal_name", "tax_id"]


@admin.register(Subsidiary)
class SubsidiaryAdmin(admin.ModelAdmin):
    list_display = ["name", "code", "company", "city", "is_active"]
    list_filter = ["company", "is_active"]
    search_fields = ["name", "code", "city"]


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ["name", "subsidiary", "manager"]
    list_filter = ["subsidiary"]
    search_fields = ["name"]
