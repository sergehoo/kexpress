from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from apps.core.mixins import TenantScopedViewSetMixin
from apps.expenses.models import Expense, FuelLog
from apps.expenses.serializers import ExpenseSerializer, FuelLogSerializer


class FuelLogViewSet(TenantScopedViewSetMixin, viewsets.ModelViewSet):
    queryset = FuelLog.objects.select_related("vehicle", "subsidiary")
    serializer_class = FuelLogSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["vehicle", "subsidiary"]
    search_fields = ["vehicle__registration"]
    ordering_fields = ["date", "amount", "liters"]

    def perform_create(self, serializer):
        log = serializer.save()
        from apps.core.enums import NotificationType
        from apps.notifications.events import finance_users, managers_of
        from apps.notifications.services import notify_many

        notify_many(
            managers_of(log.subsidiary_id) + finance_users(),
            NotificationType.FUEL_DECLARED,
            title=f"Plein déclaré — {log.vehicle.registration}",
            message=(
                f"Véhicule : {log.vehicle.registration}\n"
                f"Filiale : {log.subsidiary.name}\n"
                f"{log.liters} L pour {log.amount} XOF le {log.date:%d/%m/%Y}."
                + (f"\nCourse liée : {log.trip.destination}" if log.trip_id else "")
            ),
            link="/fuel",
        )


class ExpenseViewSet(TenantScopedViewSetMixin, viewsets.ModelViewSet):
    queryset = Expense.objects.select_related("vehicle", "subsidiary")
    serializer_class = ExpenseSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["category", "vehicle", "subsidiary"]
    search_fields = ["label"]
    ordering_fields = ["date", "amount"]

    def perform_create(self, serializer):
        exp = serializer.save()
        from apps.core.enums import NotificationType
        from apps.notifications.events import finance_users, managers_of
        from apps.notifications.services import notify_many

        notify_many(
            managers_of(exp.subsidiary_id) + finance_users(),
            NotificationType.EXPENSE_ADDED,
            title=f"Dépense ajoutée — {exp.get_category_display()}",
            message=(
                f"{exp.label} : {exp.amount} XOF le {exp.date:%d/%m/%Y}\n"
                f"Filiale : {exp.subsidiary.name}"
                + (f"\nVéhicule : {exp.vehicle.registration}" if exp.vehicle_id else "")
                + (f"\nCourse liée : {exp.trip.destination}" if exp.trip_id else "")
            ),
            link="/expenses",
        )
