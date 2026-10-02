"""Seconde revue adverse F3 + P0 — un test par défaut confirmé (et corrigé).

- notifications relues selon les droits d'AUJOURD'HUI : montants masqués sans `view_expenses`,
  alerte d'un budget qui n'est plus visible retirée (mutation, perte de la lecture groupe),
  relance d'email refusée quand le destinataire n'a plus accès ;
- un message d'erreur ne nomme pas un budget de groupe invisible ;
- l'auditeur n'écrit ni véhicule ni référentiel ;
- connexion limitée par compte visé ; création refusée si l'invitation ne peut pas arriver ;
  révocation des sessions → fermeture des WebSockets du compte ;
- budgets : dépense validée datée d'un mois à venir comptée ; mois clos avant F3 figé à sa
  première lecture ; ligne révisée à 0 et consommée → alerte ; facture de loyer = pièce du
  contrat (jamais comptée deux fois) ; nom de budget facultatif.
"""
from datetime import date
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.enums import NotificationType, RoleChoices
from apps.expenses import workflow
from apps.expenses.models import Expense
from apps.finance.budget import BudgetError, add_line, approve, check_alerts, create_budget, revise_line
from apps.finance.budget_read import dashboard, live_cells, month_cells
from apps.finance.models import BudgetActual, BudgetAlert, FinancialPeriod, VehicleAcquisition
from apps.maintenance.models import MaintenanceType
from apps.notifications.models import EmailLog, Notification
from apps.notifications.services import notify
from apps.vehicles.models import InsurancePolicy, Vehicle
from tests.test_finance_f1 import _user

pytestmark = pytest.mark.django_db

D = Decimal


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def fin_a(sub_a):
    return _user("fin-a-r2f@test.io", RoleChoices.FINANCE, sub_a)


@pytest.fixture
def group_finance(db):
    return _user("fin-g-r2f@test.io", RoleChoices.FINANCE)


def _today():
    return timezone.localdate()


def _expense(sub, author, amount, *, category="toll", day=None, status="validated", **extra):
    return Expense.objects.create(subsidiary=sub, created_by=author, category=category, label=category,
                                  amount=D(amount), date=day or _today(), status=status, **extra)


def _approved(sub, author, approver, specs, *, thresholds=(50,), year=None, name="Suivi"):
    budget = create_budget(actor=author, year=year or _today().year, name=name, subsidiary=sub,
                           alert_thresholds=list(thresholds))
    lines = [add_line(budget, actor=author, **spec) for spec in specs]
    approve(budget, actor=approver)
    return budget, lines


# =====================================================================================
# Notifications
# =====================================================================================


def test_demoted_profile_no_longer_reads_expense_amounts_in_notifications(api, sub_a, fleet_a):
    notify(fleet_a, NotificationType.EXPENSE_ADDED, title="Dépense ajoutée — Péage",
           message="Péage autoroute : 12 345 XOF le 02/10/2026\nFiliale : Abidjan")
    api.force_authenticate(fleet_a)
    assert b"12 345 XOF" in api.get("/api/notifications/").content  # contrôle positif
    fleet_a.role = RoleChoices.REQUESTER
    fleet_a.save()
    api.force_authenticate(User.objects.get(pk=fleet_a.pk))
    body = api.get("/api/notifications/").json()
    rows = body["results"] if isinstance(body, dict) else body
    assert rows and "12 345" not in rows[0]["message"] and "[montant masqué]" in rows[0]["message"]
    assert "Filiale : Abidjan" in rows[0]["message"]  # le reste de l'information demeure


def test_an_alert_of_a_budget_no_longer_visible_is_withdrawn(api, sub_a, sub_b, fleet_a, company_admin,
                                                             group_finance):
    _approved(None, company_admin, group_finance, [{"amount": "7654321", "category": "toll"}], name="Groupe R2")
    _expense(sub_a, fleet_a, "5000000")
    assert check_alerts() == 1
    api.force_authenticate(group_finance)
    assert b"Groupe R2" in api.get("/api/notifications/").content  # contrôle positif
    group_finance.subsidiary = sub_b  # perd la lecture groupe
    group_finance.save()
    api.force_authenticate(User.objects.get(pk=group_finance.pk))
    content = api.get("/api/notifications/").content
    assert b"Groupe R2" not in content and b"7654321" not in content
    assert api.get("/api/notifications/unread_count/").json()["count"] == 0


def test_an_email_is_not_resent_to_a_recipient_who_lost_access(api, sub_a, fleet_a, company_admin, fin_a):
    _approved(sub_a, fin_a, company_admin, [{"amount": "7654321", "category": "toll"}])
    _expense(sub_a, fleet_a, "5000000")
    assert check_alerts() == 1
    notification = Notification.objects.get(recipient=fleet_a, notification_type=NotificationType.BUDGET_ALERT)
    log = EmailLog.objects.create(recipient=fleet_a, to_email=fleet_a.email, subject=notification.title,
                                  notification=notification, status="sent")
    api.force_authenticate(company_admin)
    url = f"/api/notification-emails/{log.pk}/resend/"
    assert api.post(url).status_code == 200  # contrôle positif : le destinataire y a encore accès
    fleet_a.role = RoleChoices.REQUESTER
    fleet_a.save()
    response = api.post(url)
    assert response.status_code == 403 and "plus accès" in response.json()["detail"]


def test_overlap_refusal_never_names_an_invisible_group_budget(sub_a, fin_a, company_admin, group_finance):
    _approved(None, company_admin, group_finance,
              [{"amount": "1000", "subsidiary_id": sub_a.pk, "category": "toll"}], name="Confidentiel groupe")
    mine = create_budget(actor=fin_a, year=_today().year, name="Abidjan", subsidiary=sub_a)
    add_line(mine, actor=fin_a, amount="500", category="toll")
    with pytest.raises(BudgetError) as refused:
        approve(mine, actor=company_admin)
    assert "Confidentiel groupe" in str(refused.value)  # l'approbateur groupe le voit
    other = create_budget(actor=fin_a, year=_today().year, name="Abidjan v2", subsidiary=sub_a)
    add_line(other, actor=fin_a, amount="10", category="parking")
    approve(other, actor=company_admin)
    with pytest.raises(BudgetError) as hidden:
        add_line(other, actor=group_finance, amount="5", category="toll", reason="Ajout")
    assert "Confidentiel groupe" in str(hidden.value)
    from apps.finance.budget import _describe
    from apps.finance.models import BudgetLine

    line = BudgetLine.objects.get(budget__name="Confidentiel groupe")
    assert "Confidentiel" not in _describe(line, fin_a)


# =====================================================================================
# Auditeur, connexion, invitations, sockets
# =====================================================================================


def test_auditor_writes_neither_vehicles_nor_reference_types(api, sub_a):
    auditor = _user("aud-r2f@test.io", RoleChoices.AUDITOR, sub_a)
    vehicle = Vehicle.objects.create(subsidiary=sub_a, registration="R2F-1", brand="Toyota", model="Hilux")
    kind = MaintenanceType.objects.create(name="Vidange R2F")
    api.force_authenticate(auditor)
    assert api.patch(f"/api/vehicles/{vehicle.pk}/", {"mileage": 999999}, format="json").status_code == 403
    assert api.patch(f"/api/maintenance-types/{kind.pk}/", {"name": "Pneus"}, format="json").status_code == 403
    assert api.post("/api/maintenance-types/", {"name": "Pneus"}, format="json").status_code == 403
    assert api.get("/api/maintenance-types/").status_code == 200  # la lecture demeure
    vehicle.refresh_from_db()
    kind.refresh_from_db()
    assert vehicle.mileage != 999999 and kind.name == "Vidange R2F"


def test_login_is_also_limited_per_targeted_account(monkeypatch, requester_a):
    from rest_framework.throttling import ScopedRateThrottle

    from apps.accounts.views import LoginEmailThrottle

    monkeypatch.setattr(ScopedRateThrottle, "THROTTLE_RATES", {**ScopedRateThrottle.THROTTLE_RATES, "login": "100/min"})
    monkeypatch.setattr(LoginEmailThrottle, "THROTTLE_RATES", {**LoginEmailThrottle.THROTTLE_RATES, "login_email": "2/min"})
    cache.clear()
    codes = [APIClient(REMOTE_ADDR=f"10.9.0.{i}").post("/api/auth/token/", {"email": requester_a.email, "password": "x"},
                                                      format="json").status_code for i in range(3)]
    assert codes == [401, 401, 429]  # trois adresses différentes, même compte visé
    assert APIClient(REMOTE_ADDR="10.9.0.9").post("/api/auth/token/", {"email": "autre@test.io", "password": "x"},
                                                  format="json").status_code == 401
    cache.clear()


def test_account_creation_is_refused_when_the_invitation_cannot_arrive(api, settings, admin_a):
    settings.INVITATION_DELIVERY_CHECK = True
    settings.FRONTEND_URL = ""
    api.force_authenticate(admin_a)
    response = api.post("/api/employees/", {"email": "perdu@test.io", "first_name": "A", "last_name": "B",
                                            "role": "requester"}, format="json")
    assert response.status_code == 400 and "FRONTEND_URL" in response.json()["detail"]
    assert not User.objects.filter(email="perdu@test.io").exists()


def test_revocation_closes_the_accounts_websockets(fleet_a, monkeypatch, django_capture_on_commit_callbacks):
    from apps.accounts import sessions

    closed = []
    monkeypatch.setattr(sessions, "close_user_sockets", lambda user_id: closed.append(user_id))
    with django_capture_on_commit_callbacks(execute=True):
        sessions.revoke_sessions(fleet_a)
    assert closed == [fleet_a.pk]

    from apps.tracking.consumers import FleetConsumer, TripTrackingConsumer, user_group

    assert user_group(fleet_a.pk) == f"user_{fleet_a.pk}"
    assert hasattr(FleetConsumer, "session_revoked") and hasattr(TripTrackingConsumer, "session_revoked")


# =====================================================================================
# Budgets
# =====================================================================================


def test_a_validated_expense_dated_later_this_year_is_counted(sub_a, fleet_a, fin_a, company_admin):
    today = _today()
    if today.month == 12:
        pytest.skip("aucun mois à venir dans l'exercice en décembre")
    later = date(today.year, today.month + 1, 15)
    expense = _expense(sub_a, fleet_a, "40000", day=later, status="to_validate")
    workflow.validate(expense, fin_a)
    _approved(sub_a, fin_a, company_admin, [{"amount": "50000", "category": "toll"}])
    data = dashboard(fin_a, today.year)
    assert data["totals"]["realised"] == "40000.00" and data["lines"][0]["available"] == "10000.00"
    assert check_alerts() == 1


def test_a_month_closed_before_budgets_is_frozen_at_its_first_read(sub_a, fleet_a):
    _expense(sub_a, fleet_a, "700", day=date(2025, 2, 10))
    FinancialPeriod.objects.create(year=2025, month=2, status=FinancialPeriod.CLOSED)
    assert month_cells(2025, 2)[(str(sub_a.pk), None, "toll")]["realised"] == D("700")
    period = FinancialPeriod.objects.get(year=2025, month=2)
    assert period.budget_frozen_at is not None and BudgetActual.objects.filter(period=period).count() == 1
    vehicle = Vehicle.objects.create(subsidiary=sub_a, registration="R2F-2", brand="Toyota", model="Hilux")
    InsurancePolicy.objects.create(vehicle=vehicle, company="NSIA", start_date=date(2025, 1, 1),
                                   expiry_date=date(2025, 12, 31), cost=D("365000"))
    assert (str(sub_a.pk), None, "insurance") not in month_cells(2025, 2)  # le figé ne bouge plus


def test_a_line_revised_to_zero_still_alerts_when_consumed(sub_a, fleet_a, fin_a, company_admin):
    budget, (line,) = _approved(sub_a, fin_a, company_admin, [{"amount": "1000", "category": "toll"}],
                                thresholds=(80, 100))
    revise_line(line, actor=fin_a, amount="0", reason="Ligne retirée")
    _expense(sub_a, fleet_a, "300")
    assert check_alerts() == 2
    assert set(BudgetAlert.objects.filter(line=line).values_list("threshold", flat=True)) == {80, 100}
    assert dashboard(fin_a, _today().year)["lines"][0]["alert_level"] == 100


def test_a_lease_rent_invoice_is_a_piece_of_the_contract(api, sub_a, fleet_a, fin_a):
    vehicle = Vehicle.objects.create(subsidiary=sub_a, registration="R2F-3", brand="Toyota", model="Hilux")
    lease = VehicleAcquisition.objects.create(vehicle=vehicle, mode="leasing", acquisition_date=date(2025, 1, 1),
                                              monthly_payment="31000", depreciation_months=36)
    paid_at = timezone.make_aware(timezone.datetime(2025, 3, 5, 10), timezone.get_current_timezone())
    _expense(sub_a, fin_a, "31000", category="other", day=date(2025, 3, 5), status="paid", paid_at=paid_at,
             source_type="lease", source_id=lease.pk, vehicle=vehicle)
    cells = dict(live_cells(2025, 3).items())
    fixed = cells[(str(sub_a.pk), None, "fixed_charges")]
    assert (fixed["realised"], fixed["disbursed"]) == (D("31000.00"), D("31000"))
    assert (str(sub_a.pk), None, "other") not in cells  # la facture n'est pas recomptée


def test_a_budget_name_is_optional(api, sub_a, fin_a):
    api.force_authenticate(fin_a)
    response = api.post("/api/finance/budgets/", {"year": 2027, "name": ""}, format="json")
    assert response.status_code == 201, response.content
    assert response.json()["name"] == "Budget 2027"
