from django.contrib import admin

from apps.reservations.models import (
    ApprovalStep,
    ApprovalWorkflow,
    Reservation,
    ReservationAttachment,
    ReservationValidation,
)


class ApprovalStepInline(admin.TabularInline):
    model = ApprovalStep
    extra = 0


@admin.register(ApprovalWorkflow)
class ApprovalWorkflowAdmin(admin.ModelAdmin):
    list_display = ["name", "subsidiary", "is_active"]
    inlines = [ApprovalStepInline]


class ReservationValidationInline(admin.TabularInline):
    model = ReservationValidation
    extra = 0


class ReservationAttachmentInline(admin.TabularInline):
    model = ReservationAttachment
    extra = 0


@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    list_display = ["id", "requester", "subsidiary", "trip_date", "destination", "status", "priority"]
    list_filter = ["status", "priority", "subsidiary", "needs_driver"]
    search_fields = ["destination", "purpose", "requester__email"]
    date_hierarchy = "trip_date"
    inlines = [ReservationValidationInline, ReservationAttachmentInline]


@admin.register(ReservationValidation)
class ReservationValidationAdmin(admin.ModelAdmin):
    list_display = ["reservation", "level", "decision", "validator", "decided_at"]
    list_filter = ["level", "decision"]
