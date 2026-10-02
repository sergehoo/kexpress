from django.urls import path
from rest_framework.routers import DefaultRouter

from apps.finance.views_budget import BudgetDashboardView, BudgetExportView, BudgetLineViewSet, BudgetViewSet
from apps.finance.views_f2 import (
    ExpenseDashboardView,
    FinanceSettingsView,
    FinancialAdjustmentViewSet,
    ReconciliationView,
)
from apps.finance.views import (
    CostCenterViewSet,
    FinancialPeriodView,
    SubsidiaryCostView,
    TripCostListView,
    TripCostSheetView,
    TripCostStatsView,
    TripPricingRuleViewSet,
    TripPricingView,
    VehicleAcquisitionViewSet,
    VehicleChargeViewSet,
    VehicleCostListView,
    VehicleCostView,
)

router = DefaultRouter()
router.register("finance/trip-pricing-rules", TripPricingRuleViewSet, basename="trip-pricing-rule")
router.register("finance/cost-centers", CostCenterViewSet, basename="cost-center")
router.register("finance/vehicle-charges", VehicleChargeViewSet, basename="vehicle-charge")
router.register("finance/vehicle-acquisitions", VehicleAcquisitionViewSet, basename="vehicle-acquisition")
router.register("finance/adjustments", FinancialAdjustmentViewSet, basename="financial-adjustment")
router.register("finance/budgets", BudgetViewSet, basename="budget")
router.register("finance/budget-lines", BudgetLineViewSet, basename="budget-line")

urlpatterns = [
    # Avant le routeur : sinon « dashboard » serait lu comme l'identifiant d'un budget.
    path("finance/budgets/dashboard/", BudgetDashboardView.as_view(), name="budget-dashboard"),
    path("finance/budgets/export/", BudgetExportView.as_view(), name="budget-export"),
] + router.urls + [
    path("finance/trips/<uuid:trip_id>/pricing/", TripPricingView.as_view(), name="trip-pricing"),
    path("finance/trips/<uuid:trip_id>/cost/", TripCostSheetView.as_view(), name="trip-cost-sheet"),
    path("finance/trip-costs/", TripCostStatsView.as_view(), name="trip-cost-stats"),
    path("finance/trip-cost-sheets/", TripCostListView.as_view(), name="trip-cost-list"),
    path("finance/vehicle-costs/", VehicleCostListView.as_view(), name="vehicle-cost-list"),
    path("finance/vehicles/<uuid:vehicle_id>/costs/", VehicleCostView.as_view(), name="vehicle-cost"),
    path("finance/subsidiary-costs/", SubsidiaryCostView.as_view(), name="subsidiary-cost"),
    path("finance/periods/", FinancialPeriodView.as_view(), name="financial-period"),
    # F2
    path("finance/settings/", FinanceSettingsView.as_view(), name="finance-settings"),
    path("finance/reconciliation/", ReconciliationView.as_view(), name="finance-reconciliation"),
    path("finance/reconciliation/<uuid:expense_id>/", ReconciliationView.as_view(),
         name="finance-reconciliation-detail"),
    path("finance/expense-dashboard/", ExpenseDashboardView.as_view(), name="expense-dashboard"),
]
