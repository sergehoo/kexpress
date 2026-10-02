"""Correctifs de la revue adverse F3 + P0 — un test par défaut confirmé.

F3 (budgets) :
- l'ENGAGÉ d'un mois clos est relu en direct : validé, rejeté ou terminé après la clôture, il
  retombe — un montant ne compte jamais deux fois ni « pour toujours » ;
- un mois clos SANS donnée reste figé à zéro ; les engagements des mois à venir comptent ;
- décaissé d'une pièce rangé sous la catégorie de sa source ; loyer de leasing = charge fixe ;
- alertes : exercice précédent contrôlé, taux sans débordement, un budget en erreur n'arrête
  pas les autres, profil rétrogradé privé des anciennes alertes ;
- qui a saisi des lignes n'approuve pas ; saisies invalides → 400 ; exercice clos → plus de
  révision annuelle ; PATCH d'un budget tracé ; tableau de bord cohérent (budgets du périmètre,
  ventilations sous les clés des lignes) ;
- filiales : l'auditeur n'écrit pas ; une filiale qui porte des données ne se supprime pas.

P0 (comptes) :
- plafond anti-escalade : PATCH rôle + mot de passe évalué avant ET après, promotion au-delà
  du plafond → mot de passe inutilisable + sessions coupées + invitation ; email d'un compte
  plus puissant intouchable ; permissions nominatives comptées ;
- sessions JWT et liens d'invitation révoqués (mot de passe, blocage, nouvelle invitation) ;
- mots de passe connus refusés ; connexion à débit limité (adresse réelle, pas X-Forwarded-For) ;
- en production, une invitation qui ne peut pas arriver est refusée ; corps non objet → 400.
"""
import re
import time
from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group, Permission
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient, APIRequestFactory
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from apps.accounts.invitations import invitation_link
from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.core.enums import NotificationType, RoleChoices
from apps.expenses import workflow
from apps.expenses.models import Expense, FuelLog
from apps.finance import permissions as perms
from apps.finance.budget import BudgetError, add_line, approve, check_alerts, create_budget, revise_line
from apps.finance.budget_read import cells_for_year, dashboard, line_figures, live_cells, month_cells
from apps.finance.models import BudgetAlert, FinancialPeriod, VehicleAcquisition
from apps.finance.periods import close_period
from apps.maintenance.models import MaintenanceRecord, MaintenanceType
from apps.notifications.models import Notification
from apps.organizations.models import Subsidiary
from apps.vehicles.models import InsurancePolicy, Vehicle
from tests.test_finance_f1 import _user

pytestmark = pytest.mark.django_db

D = Decimal
STRONG = "Tourne-Clef-Solide-41"


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def fin_a(sub_a):
    return _user("fin-a-rvf@test.io", RoleChoices.FINANCE, sub_a)


@pytest.fixture
def group_finance(db):
    return _user("fin-g-rvf@test.io", RoleChoices.FINANCE)


@pytest.fixture
def vehicle(sub_a):
    return Vehicle.objects.create(subsidiary=sub_a, registration="RVF-1", brand="Toyota", model="Hilux")


def _expense(sub, author, amount, *, category="parking", day=None, status="validated", **extra):
    return Expense.objects.create(subsidiary=sub, created_by=author, category=category, label=category,
                                  amount=D(amount), date=day or timezone.localdate(), status=status, **extra)


def _key(sub, category):
    return (str(sub.pk), None, category)


def _year_line(budget_year, sub, author, category="parking", amount="100000"):
    budget = create_budget(actor=author, year=budget_year, name=f"B{budget_year}", subsidiary=sub)
    return add_line(budget, actor=author, amount=amount, category=category)


# =====================================================================================
# F3 — engagé d'un mois clos relu en direct
# =====================================================================================


def test_engaged_of_a_closed_month_is_released_when_validated_late(sub_a, fleet_a, fin_a, company_admin):
    late = _expense(sub_a, fleet_a, "1000", day=date(2025, 3, 20), status="to_validate")
    close_period(2025, 3, company_admin)
    line_2025 = _year_line(2025, sub_a, fin_a)
    assert line_figures(line_2025, cells_for_year(2025))["engaged"] == D("1000")

    workflow.validate(late, fin_a)
    late.refresh_from_db()
    assert late.adjustment.status == "approved"
    figures_2025 = line_figures(line_2025, cells_for_year(2025))
    assert (figures_2025["engaged"], figures_2025["realised"]) == (D("0"), D("0"))
    today = timezone.localdate()
    assert dict(live_cells(today.year, today.month).items())[_key(sub_a, "parking")]["realised"] == D("1000")


def test_engaged_of_a_closed_month_is_released_when_rejected(sub_a, fleet_a, fin_a, company_admin):
    late = _expense(sub_a, fleet_a, "500", day=date(2025, 3, 20), status="to_validate")
    close_period(2025, 3, company_admin)
    workflow.reject(late, fin_a, "Doublon")
    line = _year_line(2025, sub_a, fin_a)
    assert line_figures(line, cells_for_year(2025))["engaged"] == D("0")


def test_planned_maintenance_completed_after_close_counts_once(sub_a, fin_a, company_admin, vehicle):
    kind = MaintenanceType.objects.create(name="Vidange RVF")
    record = MaintenanceRecord.objects.create(vehicle=vehicle, subsidiary=sub_a, maintenance_type=kind,
                                              status="planned", scheduled_date=date(2025, 3, 28), cost="9000")
    close_period(2025, 3, company_admin)
    record.status, record.performed_date = "completed", timezone.localdate()
    record.save()
    line = _year_line(2025, sub_a, fin_a, category="maintenance")
    assert line_figures(line, cells_for_year(2025))["engaged"] == D("0")
    today = timezone.localdate()
    cell = dict(live_cells(today.year, today.month).items())[_key(sub_a, "maintenance")]
    assert (cell["realised"], cell["engaged"]) == (D("9000"), D("0"))


def test_an_engagement_entered_after_close_shows_in_its_closed_month(sub_a, fleet_a, company_admin):
    close_period(2025, 3, company_admin)
    _expense(sub_a, fleet_a, "250", day=date(2025, 3, 15), status="submitted")
    assert month_cells(2025, 3)[_key(sub_a, "parking")]["engaged"] == D("250")


def test_a_closed_month_without_data_stays_frozen_at_zero(sub_a, company_admin, vehicle):
    close_period(2025, 2, company_admin)
    assert FinancialPeriod.objects.get(year=2025, month=2).budget_frozen_at is not None
    InsurancePolicy.objects.create(vehicle=vehicle, company="NSIA", start_date=date(2025, 1, 1),
                                   expiry_date=date(2025, 12, 31), cost=D("365000"))
    assert not any(values["realised"] for values in month_cells(2025, 2).values())
    assert month_cells(2025, 4)[_key(sub_a, "insurance")]["realised"] == D("30000.00")  # mois ouvert


def test_future_commitments_are_engaged_but_never_realised(sub_a, fin_a, vehicle):
    today = timezone.localdate()
    if today.month == 12:
        pytest.skip("aucun mois à venir dans l'exercice en décembre")
    kind = MaintenanceType.objects.create(name="Pneus RVF")
    MaintenanceRecord.objects.create(vehicle=vehicle, subsidiary=sub_a, maintenance_type=kind, status="planned",
                                     scheduled_date=date(today.year, today.month + 1, 5), cost="12000")
    InsurancePolicy.objects.create(vehicle=vehicle, company="NSIA", start_date=date(today.year, 1, 1),
                                   expiry_date=date(today.year, 12, 31), cost=D("365000"))
    line = _year_line(today.year, sub_a, fin_a, category="maintenance", amount="100000")
    data = dashboard(fin_a, today.year)
    assert data["totals"]["engaged"] == "12000.00"
    assert data["lines"][0]["available"] == "88000.00"
    future = cells_for_year(today.year)[today.month + 1]
    assert all(values["realised"] == 0 and values["disbursed"] == 0 for values in future.values())
    assert line.pk


# =====================================================================================
# F3 — décaissé d'une pièce, leasing
# =====================================================================================


def test_paid_maintenance_piece_is_disbursed_under_maintenance(sub_a, fleet_a, vehicle):
    today = timezone.localdate()
    kind = MaintenanceType.objects.create(name="Freins RVF")
    record = MaintenanceRecord.objects.create(vehicle=vehicle, subsidiary=sub_a, maintenance_type=kind,
                                              status="completed", performed_date=today, cost="15000")
    _expense(sub_a, fleet_a, "15000", category="repair", status="paid", paid_at=timezone.now(),
             source_type="maintenance", source_id=record.pk, vehicle=vehicle)
    cells = dict(live_cells(today.year, today.month).items())
    assert cells[_key(sub_a, "maintenance")]["realised"] == D("15000")
    assert cells[_key(sub_a, "maintenance")]["disbursed"] == D("15000")
    assert _key(sub_a, "repair") not in cells


def test_leasing_rent_is_a_fixed_charge_and_purchase_depreciation_is_not(sub_a, vehicle):
    VehicleAcquisition.objects.create(vehicle=vehicle, mode="leasing", acquisition_date=date(2025, 1, 1),
                                      monthly_payment="31000", depreciation_months=36)
    bought = Vehicle.objects.create(subsidiary=sub_a, registration="RVF-2", brand="Toyota", model="Hilux")
    VehicleAcquisition.objects.create(vehicle=bought, mode="purchase", acquisition_date=date(2025, 1, 1),
                                      purchase_price="3600000", depreciation_months=36)
    cells = dict(live_cells(2025, 3).items())
    assert cells[_key(sub_a, "fixed_charges")]["realised"] == D("31000.00")


# =====================================================================================
# F3 — alertes
# =====================================================================================


def _approved(sub, author, approver, year, specs, thresholds):
    budget = create_budget(actor=author, year=year, name=f"Suivi {year} {specs[0]['category']}",
                           subsidiary=sub, alert_thresholds=thresholds)
    for spec in specs:
        add_line(budget, actor=author, **spec)
    approve(budget, actor=approver)
    return budget


def test_previous_year_budget_is_still_alerted(sub_a, fleet_a, fin_a, company_admin):
    last_year = timezone.localdate().year - 1
    _approved(sub_a, fin_a, company_admin, last_year, [{"amount": "1000", "category": "toll"}], [50])
    _expense(sub_a, fleet_a, "800", category="toll", day=date(last_year, 12, 31))
    assert check_alerts() == 1


def test_huge_rate_is_stored_and_a_failing_budget_does_not_stop_the_others(
        sub_a, fleet_a, fin_a, company_admin, monkeypatch):
    year = timezone.localdate().year
    tiny = _approved(sub_a, fin_a, company_admin, year, [{"amount": "1", "category": "toll"}], [100])
    _approved(sub_a, fin_a, company_admin, year, [{"amount": "1000", "category": "parking"}], [50])
    _expense(sub_a, fleet_a, "1000", category="toll")
    _expense(sub_a, fleet_a, "600", category="parking")
    assert check_alerts() == 2
    assert BudgetAlert.objects.get(line__budget=tiny).rate == D("100000.00")

    BudgetAlert.objects.all().delete()
    from apps.finance import budget as service

    real = service._check_budget_alerts

    def failing(budget, cache_):
        if budget.pk == tiny.pk:
            raise RuntimeError("panne simulée")
        return real(budget, cache_)

    monkeypatch.setattr(service, "_check_budget_alerts", failing)
    assert check_alerts() == 1  # le budget sain est contrôlé malgré la panne de l'autre


def test_demoted_profile_no_longer_reads_budget_alerts(api, sub_a, fleet_a, fin_a, company_admin):
    year = timezone.localdate().year
    _approved(sub_a, fin_a, company_admin, year, [{"amount": "7654321", "category": "toll"}], [50])
    _expense(sub_a, fin_a, "5000000", category="toll")
    assert check_alerts() == 1
    assert Notification.objects.filter(recipient=fleet_a, notification_type=NotificationType.BUDGET_ALERT).exists()
    api.force_authenticate(fleet_a)
    assert b"7654321" in api.get("/api/notifications/").content  # contrôle positif
    fleet_a.role = RoleChoices.REQUESTER
    fleet_a.save()
    api.force_authenticate(User.objects.get(pk=fleet_a.pk))
    assert b"7654321" not in api.get("/api/notifications/").content
    assert api.get("/api/notifications/unread_count/").json()["count"] == 0


# =====================================================================================
# F3 — approbation, saisies, exercice clos, audit, tableau de bord
# =====================================================================================


def test_whoever_entered_lines_cannot_approve_them(sub_a, fin_a, group_finance, company_admin):
    budget = create_budget(actor=fin_a, year=timezone.localdate().year, name="Saisi", subsidiary=sub_a)
    add_line(budget, actor=group_finance, amount="1000", category="toll")
    with pytest.raises(BudgetError, match="autre personne"):
        approve(budget, actor=group_finance)
    approve(budget, actor=company_admin)


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-Infinity", "sNaN", "1e20", True, ""])
def test_invalid_amounts_answer_400(api, sub_a, fin_a, amount):
    api.force_authenticate(fin_a)
    budget = create_budget(actor=fin_a, year=timezone.localdate().year, name="Saisie", subsidiary=sub_a)
    line = add_line(budget, actor=fin_a, amount="10", category="toll")
    assert api.post(f"/api/finance/budgets/{budget.pk}/lines/", {"amount": amount, "category": "parking"},
                    format="json").status_code == 400
    assert api.patch(f"/api/finance/budget-lines/{line.pk}/", {"amount": amount},
                     format="json").status_code == 400


def test_non_object_bodies_answer_400(api, sub_a, fin_a):
    api.force_authenticate(fin_a)
    budget = create_budget(actor=fin_a, year=timezone.localdate().year, name="Corps", subsidiary=sub_a)
    line = add_line(budget, actor=fin_a, amount="10", category="toll")
    assert api.post(f"/api/finance/budgets/{budget.pk}/lines/", [1, 2], format="json").status_code == 400
    assert api.patch(f"/api/finance/budget-lines/{line.pk}/", ["x"], format="json").status_code == 400
    assert api.patch(f"/api/finance/budget-lines/{line.pk}/", {"amount": "20", "reason": ["x"]},
                     format="json").status_code == 200  # motif non texte ignoré (brouillon)
    assert APIClient().post("/api/auth/password-setup/", [1], format="json").status_code == 400


def test_a_fully_closed_year_freezes_annual_lines(sub_a, fin_a, company_admin):
    budget = create_budget(actor=fin_a, year=2025, name="2025", subsidiary=sub_a)
    annual = add_line(budget, actor=fin_a, amount="1200", category="toll")
    approve(budget, actor=company_admin)
    revise_line(annual, actor=fin_a, amount="1300", reason="Avant clôture")
    for month in range(1, 13):
        close_period(2025, month, company_admin)
    with pytest.raises(BudgetError, match="entièrement clos"):
        revise_line(annual, actor=fin_a, amount="1400", reason="Après coup")
    with pytest.raises(BudgetError, match="entièrement clos"):
        add_line(budget, actor=fin_a, amount="10", category="parking", reason="Après coup")


def test_an_archived_budget_never_changes(api, sub_a, fin_a, company_admin):
    from apps.finance.budget import archive

    budget = create_budget(actor=fin_a, year=timezone.localdate().year, name="Archivé", subsidiary=sub_a)
    line = add_line(budget, actor=fin_a, amount="1000", category="toll")
    approve(budget, actor=company_admin)
    archive(budget, actor=company_admin)
    with pytest.raises(BudgetError, match="archivé"):
        revise_line(line, actor=fin_a, amount="2000", reason="Après archivage")
    with pytest.raises(BudgetError, match="archivé"):
        add_line(budget, actor=fin_a, amount="10", category="parking", reason="Après archivage")
    api.force_authenticate(fin_a)
    assert api.patch(f"/api/finance/budgets/{budget.pk}/", {"name": "Renommé"}, format="json").status_code == 400
    line.refresh_from_db()
    assert line.amount == D("1000")


def test_budget_patch_is_audited(api, sub_a, fin_a):
    api.force_authenticate(fin_a)
    budget = create_budget(actor=fin_a, year=timezone.localdate().year, name="Avant", subsidiary=sub_a)
    assert api.patch(f"/api/finance/budgets/{budget.pk}/", {"name": "Après", "alert_thresholds": [70]},
                     format="json").status_code == 200
    entry = AuditLog.objects.filter(changes__action="budget_update").latest("pk")
    assert entry.changes["before"]["name"] == "Avant" and entry.changes["after"]["name"] == "Après"


def test_dashboard_lists_only_budgets_of_the_filtered_subsidiary(sub_a, sub_b, company_admin):
    year = timezone.localdate().year
    create_budget(actor=company_admin, year=year, name="Abidjan", subsidiary=sub_a)
    create_budget(actor=company_admin, year=year, name="Dakar", subsidiary=sub_b)
    data = dashboard(company_admin, year, subsidiary=str(sub_b.pk))
    assert [b["name"] for b in data["budgets"]] == ["Dakar"]


def test_breakdowns_use_the_keys_of_the_lines(sub_a, sub_b, fin_a, company_admin):
    create = create_budget(actor=fin_a, year=2025, name="Enveloppe", subsidiary=sub_a)
    add_line(create, actor=fin_a, amount="1200", label="Toutes dépenses")
    _expense(sub_a, fin_a, "100", category="toll", day=date(2025, 3, 10))
    data = dashboard(fin_a, 2025)
    assert {c["key"]: (c["planned"], c["realised"]) for c in data["by_category"]} == {"all": ("1200.00", "100.00")}

    group = create_budget(actor=company_admin, year=2025, name="Groupe", subsidiary=None)
    add_line(group, actor=company_admin, amount="5000", category="parking")
    _expense(sub_b, company_admin, "300", category="parking", day=date(2025, 4, 10))
    subs = {s["label"]: (s["planned"], s["realised"]) for s in dashboard(company_admin, 2025)["by_subsidiary"]}
    assert subs["Groupe"] == ("5000.00", "300.00") and subs["Abidjan"] == ("1200.00", "100.00")


# =====================================================================================
# Filiales
# =====================================================================================


def test_auditor_never_writes_subsidiaries(api, company, sub_a):
    api.force_authenticate(_user("aud-rvf@test.io", RoleChoices.AUDITOR))
    assert api.post("/api/subsidiaries/", {"name": "Bouaké", "code": "BKE"}, format="json").status_code == 403
    assert api.patch(f"/api/subsidiaries/{sub_a.pk}/", {"name": "X"}, format="json").status_code == 403
    assert api.delete(f"/api/subsidiaries/{sub_a.pk}/").status_code == 403
    assert Subsidiary.objects.get(pk=sub_a.pk).name == "Abidjan"


def test_a_subsidiary_with_data_is_never_deleted(api, company, company_admin):
    full = Subsidiary.objects.create(company=company, name="Bouaké", code="BKE")
    finance = _user("fin-bke-rvf@test.io", RoleChoices.FINANCE, full)
    empty = Subsidiary.objects.create(company=company, name="Vide", code="VID")
    api.force_authenticate(company_admin)
    assert api.delete(f"/api/subsidiaries/{full.pk}/").status_code == 400
    finance.refresh_from_db()
    assert finance.subsidiary_id == full.pk and not finance.is_group_finance
    assert api.delete(f"/api/subsidiaries/{empty.pk}/").status_code == 204


# =====================================================================================
# P0 — plafond anti-escalade
# =====================================================================================


def _bearer(user, *, age=10):
    """Jeton d'accès émis il y a `age` secondes (avant toute révocation faite maintenant)."""
    access = AccessToken.for_user(user)
    access["iat"] = int(time.time()) - age
    return str(access)


def _refresh(user, *, age=10):
    refresh = RefreshToken.for_user(user)
    refresh["iat"] = int(time.time()) - age
    return str(refresh)


def test_demote_with_password_is_refused_on_a_stronger_account(api, admin_a, fin_a):
    api.force_authenticate(admin_a)
    response = api.patch(f"/api/employees/{fin_a.pk}/", {"role": "requester", "password": STRONG}, format="json")
    assert response.status_code == 403
    fin_a.refresh_from_db()
    assert fin_a.role == RoleChoices.FINANCE and not fin_a.check_password(STRONG)


def test_promotion_beyond_the_ceiling_resets_credentials(api, admin_a, requester_a, mailoutbox,
                                                         django_capture_on_commit_callbacks):
    old_token = _bearer(requester_a)
    api.force_authenticate(admin_a)
    assert api.post(f"/api/employees/{requester_a.pk}/set-password/", {"password": STRONG},
                    format="json").status_code == 200
    with django_capture_on_commit_callbacks(execute=True):
        response = api.patch(f"/api/employees/{requester_a.pk}/", {"role": "finance"}, format="json")
    assert response.status_code == 200, response.content
    user = User.objects.get(pk=requester_a.pk)
    assert user.role == RoleChoices.FINANCE
    assert not user.has_usable_password() and not user.check_password(STRONG)
    assert [m.to for m in mailoutbox][-1] == [user.email]
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {old_token}")
    assert client.get("/api/auth/me/").status_code == 401


def test_email_of_a_stronger_account_is_untouchable(api, admin_a, fin_a, requester_a):
    api.force_authenticate(admin_a)
    assert api.patch(f"/api/employees/{fin_a.pk}/", {"email": "pirate@evil.io"}, format="json").status_code == 403
    assert User.objects.get(pk=fin_a.pk).email != "pirate@evil.io"
    assert api.patch(f"/api/employees/{requester_a.pk}/", {"email": "nouvelle@test.io"},
                     format="json").status_code == 200


def test_nominative_finance_rights_count_in_the_ceiling(api, admin_a, requester_a):
    group = Group.objects.create(name="valideurs-rvf")
    group.permissions.add(Permission.objects.get(content_type__app_label="finance",
                                                 codename=perms.VALIDATE_EXPENSE))
    requester_a.groups.add(group)
    api.force_authenticate(admin_a)
    assert api.post(f"/api/employees/{requester_a.pk}/set-password/", {"password": STRONG},
                    format="json").status_code == 403
    assert api.post(f"/api/employees/{requester_a.pk}/reset-password/", {}, format="json").status_code == 403


# =====================================================================================
# P0 — révocation des sessions et des liens
# =====================================================================================


def test_password_set_by_admin_revokes_tokens(admin_a, fleet_a):
    access, refresh = _bearer(fleet_a), _refresh(fleet_a)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    assert client.get("/api/auth/me/").status_code == 200  # contrôle positif
    admin = APIClient()
    admin.force_authenticate(admin_a)
    assert admin.post(f"/api/employees/{fleet_a.pk}/set-password/", {"password": STRONG},
                      format="json").status_code == 200
    assert client.get("/api/auth/me/").status_code == 401
    assert APIClient().post("/api/auth/refresh/", {"refresh": refresh}, format="json").status_code == 401


def test_own_password_change_keeps_this_session_and_cuts_the_others(requester_a):
    requester_a.set_password(STRONG)
    requester_a.save()
    other_device = _bearer(requester_a)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {_bearer(requester_a)}")
    response = client.post("/api/auth/change-password/", {"current_password": STRONG,
                                                          "new_password": "Autre-Clef-Neuve-52"}, format="json")
    assert response.status_code == 200 and response.json()["access"]
    stale = APIClient()
    stale.credentials(HTTP_AUTHORIZATION=f"Bearer {other_device}")
    assert stale.get("/api/auth/me/").status_code == 401
    fresh = APIClient()
    fresh.credentials(HTTP_AUTHORIZATION=f"Bearer {response.json()['access']}")
    assert fresh.get("/api/auth/me/").status_code == 200


def test_blocking_revokes_tokens_links_and_websocket(api, admin_a, fleet_a):
    from apps.tracking.ws_auth import _get_user

    fleet_a.set_unusable_password()
    fleet_a.save()
    link = invitation_link(fleet_a)
    uid, token = re.search(r"uid=([^&]+)&token=([^&\s]+)", link).groups()
    access = _bearer(fleet_a)
    assert _get_user.func(access).pk == fleet_a.pk  # contrôle positif
    api.force_authenticate(admin_a)
    assert api.post(f"/api/employees/{fleet_a.pk}/block/").status_code == 200
    assert api.post(f"/api/employees/{fleet_a.pk}/unblock/").status_code == 200
    assert APIClient().get("/api/auth/password-setup/", {"uid": uid, "token": token}).status_code == 400
    assert not getattr(_get_user.func(access), "pk", None)


def test_a_new_invitation_voids_the_previous_links(api, admin_a, fleet_a, mailoutbox):
    fleet_a.set_unusable_password()
    fleet_a.save()
    api.force_authenticate(admin_a)
    assert api.post(f"/api/employees/{fleet_a.pk}/invite/").status_code == 200
    first = re.search(r"uid=([^&]+)&token=([^&\s]+)", mailoutbox[-1].body).groups()
    time.sleep(0.01)
    assert api.post(f"/api/employees/{fleet_a.pk}/invite/").status_code == 200
    second = re.search(r"uid=([^&]+)&token=([^&\s]+)", mailoutbox[-1].body).groups()
    anonymous = APIClient()
    assert anonymous.get("/api/auth/password-setup/", {"uid": first[0], "token": first[1]}).status_code == 400
    assert anonymous.get("/api/auth/password-setup/", {"uid": second[0], "token": second[1]}).status_code == 200


# =====================================================================================
# P0 — mots de passe connus, débit, acheminement
# =====================================================================================


@pytest.mark.parametrize("known", ["Demo1234!", "KExpress2026", "kaydan-express-9", "ChangeMe!!"])
def test_known_passwords_are_refused_everywhere(api, admin_a, fleet_a, known):
    api.force_authenticate(admin_a)
    assert api.post(f"/api/employees/{fleet_a.pk}/set-password/", {"password": known},
                    format="json").status_code == 400
    fleet_a.set_password(STRONG)
    fleet_a.save()
    api.force_authenticate(fleet_a)
    assert api.post("/api/auth/change-password/", {"current_password": STRONG, "new_password": known},
                    format="json").status_code == 400


def test_login_is_rate_limited_on_the_real_address(monkeypatch, requester_a):
    from rest_framework.throttling import ScopedRateThrottle

    monkeypatch.setattr(ScopedRateThrottle, "THROTTLE_RATES", {**ScopedRateThrottle.THROTTLE_RATES, "login": "3/min"})
    cache.clear()
    client = APIClient()
    codes = [client.post("/api/auth/token/", {"email": requester_a.email, "password": "faux"}, format="json",
                         HTTP_X_FORWARDED_FOR=f"10.0.0.{i}").status_code for i in range(4)]
    assert codes[:3] == [401, 401, 401] and codes[3] == 429
    cache.clear()

    factory = APIRequestFactory()
    idents = {ScopedRateThrottle().get_ident(factory.get("/", HTTP_X_FORWARDED_FOR=f"1.2.3.{i}")) for i in range(3)}
    assert len(idents) == 1  # X-Forwarded-For ignoré sans proxy déclaré


def test_undeliverable_invitation_is_refused_in_production(api, settings, admin_a, fleet_a, mailoutbox):
    settings.INVITATION_DELIVERY_CHECK = True
    api.force_authenticate(admin_a)
    settings.FRONTEND_URL = "http://localhost:3000"
    response = api.post(f"/api/employees/{fleet_a.pk}/invite/")
    assert response.status_code == 400 and "FRONTEND_URL" in response.json()["detail"]
    settings.FRONTEND_URL = "https://kexpress.example"
    settings.EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
    assert api.post(f"/api/employees/{fleet_a.pk}/invite/").status_code == 400
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    assert api.post(f"/api/employees/{fleet_a.pk}/invite/").status_code == 200
    assert mailoutbox[-1].to == [fleet_a.email] and "https://kexpress.example/auth/setup-password" in mailoutbox[-1].body


def test_paid_fuel_piece_never_double_counts_energy(sub_a, fleet_a, vehicle):
    """Garde-fou : la pièce payée d'un plein n'ajoute rien au réalisé énergie."""
    today = timezone.localdate()
    fuel = FuelLog.objects.create(vehicle=vehicle, subsidiary=sub_a, date=today, liters="10", amount="8000")
    _expense(sub_a, fleet_a, "8000", category="fuel", status="paid", paid_at=timezone.now(),
             source_type="fuel_log", source_id=fuel.pk, vehicle=vehicle)
    cell = dict(live_cells(today.year, today.month).items())[_key(sub_a, "energy")]
    assert (cell["realised"], cell["disbursed"]) == (D("8000"), D("8000"))
