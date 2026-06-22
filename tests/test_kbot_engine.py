"""K-BOT : le classifieur d'intentions doit reconnaître les questions standard
(et ne PAS retomber sur l'aide générique « help »)."""
import pytest

from apps.accounts.models import User
from apps.core.enums import RoleChoices
from apps.kbot.engine import answer_question
from apps.organizations.models import Company, Subsidiary


@pytest.fixture
def admin(db):
    c = Company.objects.create(name="Kaydan")
    s = Subsidiary.objects.create(company=c, name="Plateau", code="PLT")
    return User.objects.create_user(
        email="admin@k.ci", password="x", role=RoleChoices.COMPANY_ADMIN, subsidiary=s,
    )


@pytest.mark.django_db
@pytest.mark.parametrize("question, expected_intent", [
    ("Donne-moi le résumé du jour", "today_summary"),
    ("Quels véhicules sont disponibles ?", "available_vehicles"),
    ("Quels chauffeurs sont disponibles ?", "available_drivers"),
    ("Quelles réservations sont en attente ?", "pending_reservations"),
    ("Quelles maintenances arrivent à échéance ?", "upcoming_maintenance"),
    ("Quels sont les coûts du mois ?", None),  # intent variable, mais PAS « help »
])
def test_intent_recognized(admin, question, expected_intent):
    payload = answer_question(admin, question)
    assert payload["intent"] != "help", f"Tombé sur l'aide pour : {question!r} → {payload}"
    if expected_intent:
        assert payload["intent"] == expected_intent, payload
