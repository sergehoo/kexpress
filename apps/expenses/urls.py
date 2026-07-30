from rest_framework.routers import DefaultRouter

from apps.expenses.views import ElectricChargeViewSet, ExpenseViewSet, FuelLogViewSet

router = DefaultRouter()
# Le chemin `fuel` reste inchangé (contrat d'API existant) même si le module s'appelle
# désormais « Gestion de l'énergie » côté interface.
router.register("fuel", FuelLogViewSet, basename="fuel")
router.register("electric-charges", ElectricChargeViewSet, basename="electric-charge")
router.register("expenses", ExpenseViewSet, basename="expense")

urlpatterns = router.urls
