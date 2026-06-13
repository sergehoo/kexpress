from rest_framework.routers import DefaultRouter

from apps.expenses.views import ExpenseViewSet, FuelLogViewSet

router = DefaultRouter()
router.register("fuel", FuelLogViewSet, basename="fuel")
router.register("expenses", ExpenseViewSet, basename="expense")

urlpatterns = router.urls
