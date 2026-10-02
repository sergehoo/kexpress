"""Purge de recette : seuls les comptes qa.*@kaydan.test et leurs données partent ; les verrous
d'historique Car Plan sont rétablis après."""
from datetime import timedelta

import pytest
from django.core.management import CommandError, call_command
from django.db import IntegrityError, transaction

from apps.carplan import services
from apps.carplan.models import CarPlanAssignment, CarPlanEvent, CarPlanProfile, EmployeeCategory
from apps.core.enums import RoleChoices
from tests.test_carplan_c1 import TODAY, _active, _vehicle, category, eligible, employee, fleet_admin, policy  # noqa: F401
from tests.test_finance_f1 import _user

pytestmark = pytest.mark.django_db


def test_purge_removes_only_qa_data_and_restores_locks(sub_a, policy, eligible, fleet_a, fleet_admin, category):  # noqa: F811
    from apps.accounts.models import User

    real = _active(eligible, policy, _vehicle(sub_a, "REAL-01"), fleet_a, fleet_admin)
    qa = _user("qa.tester@kaydan.test", RoleChoices.REQUESTER, sub_a)
    CarPlanProfile.objects.create(user=qa, category=category)
    qa_assignment = services.request_assignment(actor=fleet_a, beneficiary=qa, policy=policy,
                                                assignment_type="company_car", start_date=TODAY + timedelta(days=200),
                                                planned_end_date=TODAY + timedelta(days=260))
    services.validate_assignment(qa_assignment, actor=fleet_admin)
    call_command("purge_qa_data")  # à blanc
    assert User.objects.filter(pk=qa.pk).exists()
    call_command("purge_qa_data", "--confirm")
    assert not User.objects.filter(pk=qa.pk).exists()
    assert not CarPlanAssignment.objects.filter(pk=qa_assignment.pk).exists()
    assert CarPlanAssignment.objects.filter(pk=real.pk).exists() and real.events.exists()
    assert EmployeeCategory.objects.filter(pk=category.pk).exists()
    with pytest.raises(IntegrityError), transaction.atomic():  # trigger rétabli
        CarPlanEvent.objects.filter(assignment=real).update(note="falsifié")


def test_purge_refuses_when_finance_data_is_linked(sub_a, fleet_a):
    from apps.expenses.models import Expense

    qa = _user("qa.finance@kaydan.test", RoleChoices.FINANCE, sub_a)
    Expense.objects.create(subsidiary=sub_a, category="toll", label="Péage", amount="100", date=TODAY,
                           status="draft", created_by=qa)
    with pytest.raises(CommandError, match="financières"):
        call_command("purge_qa_data", "--confirm")
