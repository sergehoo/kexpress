"""F2 — Circuit des dépenses : statuts, séparation des tâches, justificatifs, traçabilité.

Garanties obligatoires couvertes ici, chacune par des tests qui échoueraient si on la retirait :

1. circuit BROUILLON → SOUMISE → À VALIDER → VALIDÉE → PAYÉE par l'API, chaque geste par
   son acteur (le gestionnaire de flotte saisit et soumet, une Finance valide, une Finance
   paie) ; horodatages, valideur, payeur et références de paiement posés ; la dépense n'est
   comptée (`Expense.objects.countable()`) qu'à partir de VALIDÉE ;
2. transitions interdites refusées SANS RIEN ÉCRIRE (ni statut, ni historique, ni audit) ;
   statut en lecture seule au PATCH ; montant figé après validation ; séparation des tâches
   (l'auteur ne valide ni ne paie sa dépense : 403 « segregation ») ; barrières de rôle
   (le gestionnaire de flotte ne valide/paie/annule pas ; la Finance de filiale ne clôture ni
   ne paramètre ni ne touche au barème ; l'admin entreprise ne paie pas) ;
4. justificatif exigé par seuil : 50 000 bloque à SOUMISE jusqu'au dépôt d'une pièce par
   l'API, 49 999,99 n'en exige pas, seuil 0 = toujours, seuil vide = jamais automatique,
   exigence manuelle par « demander un complément » même sous le seuil ;
6. validation auditée : une ligne `ExpenseStatusHistory` par transition (utilisateur, date,
   ancien → nouveau statut, commentaire/motif, montant et centre de coût AU MOMENT de
   l'action), une entrée `AuditLog` « expense_<action> », historique immuable ;
15. centre de coût prérempli (course → demandeur → service → centre de coût actif), modifiable
   en brouillon/soumise/à valider, valeur finale tracée à la validation, suggestion servie par
   l'API, centre d'une autre filiale refusé.

Compléments : « demander un complément » renvoie à SOUMISE avec son commentaire ; un rejet
exige un motif ; l'annulation d'une dépense validée d'un mois ouvert la retire des coûts (et
reste refusée, avec proposition d'ajustement, sur un mois clos) ; DELETE d'un brouillon =
abandon tracé (la ligne reste) ; DELETE d'une dépense entrée dans le circuit refusé.

Mois de référence des scénarios « mois clos » : mars 2025.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.db.models import ProtectedError, Sum
from django.utils import timezone
from rest_framework.test import APIClient

from apps.audit.models import AuditLog
from apps.core.enums import AuditAction, ReservationStatus, RoleChoices, TripType
from apps.expenses.models import Expense, ExpenseStatusHistory
from apps.finance.models import (
    CostAllocation, CostCenter, FinanceSettings, FinancialAdjustment, FinancialPeriod,
    TripPricingRule,
)
from apps.finance.periods import close_period
from apps.organizations.models import Department
from apps.trips.models import Trip
from apps.vehicles.models import Vehicle
from tests.test_finance_f1 import _user

pytestmark = pytest.mark.django_db

PAY = {"payment_reference": "VIR-2026-0042", "payment_method": "transfer",
       "accounting_reference": "SAP-4500012"}


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    """Les justificatifs déposés pendant les tests ne touchent pas le vrai MEDIA_ROOT."""
    settings.MEDIA_ROOT = tmp_path


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def fin_a(sub_a):
    """Finance de filiale : valide, paie, annule — ne clôture ni ne paramètre."""
    return _user("fin-a-wf@test.io", RoleChoices.FINANCE, sub_a)


@pytest.fixture
def fin_a2(sub_a):
    return _user("fin-a2-wf@test.io", RoleChoices.FINANCE, sub_a)


@pytest.fixture
def group_finance(db):
    return _user("fin-groupe-wf@test.io", RoleChoices.FINANCE)


@pytest.fixture
def auditor(db):
    return _user("audit-wf@test.io", RoleChoices.AUDITOR)


@pytest.fixture
def fleet_a2(sub_a):
    """Second gestionnaire de flotte : auteur neutre pour isoler les barrières de rôle de la
    séparation des tâches."""
    return _user("fleet2-wf@test.io", RoleChoices.FLEET_MANAGER, sub_a)


# --- Outils -----------------------------------------------------------------------------


def _today():
    return timezone.localdate()


def _threshold(value):
    """Seuil de justificatif (FinanceSettings du groupe)."""
    settings_row = FinanceSettings.current()
    settings_row.receipt_required_from = value
    settings_row.save()


def _create(api, user, **payload):
    """Création par l'API (toujours en BROUILLON) — renvoie la ligne en base."""
    body = {"category": "toll", "label": "Péage autoroute du Nord", "amount": "15000",
            "date": _today().isoformat()}
    body.update(payload)
    api.force_authenticate(user)
    response = api.post("/api/expenses/", body, format="json")
    assert response.status_code == 201, response.content
    return Expense.objects.get(pk=response.json()["id"])


def _expense(sub, author, status, *, amount="15000", day=None, **extra):
    """Dépense posée directement dans un statut (invariants du circuit, hors parcours)."""
    return Expense.objects.create(subsidiary=sub, created_by=author, category="toll",
                                  label="Péage", amount=amount, date=day or _today(),
                                  status=status, **extra)


def _act(api, user, expense, action, **data):
    api.force_authenticate(user)
    return api.post(f"/api/expenses/{expense.pk}/{action}/", data, format="json")


def _upload(api, user, expense, name="ticket.pdf", kind="receipt"):
    api.force_authenticate(user)
    upload = SimpleUploadedFile(name, b"%PDF-1.4 justificatif de test", content_type="application/pdf")
    return api.post(f"/api/expenses/{expense.pk}/attachments/", {"file": upload, "kind": kind},
                    format="multipart")


def _counted(expense) -> bool:
    return Expense.objects.countable().filter(pk=expense.pk).exists()


def _history(expense):
    return list(ExpenseStatusHistory.objects.filter(expense_id=expense.pk)
                .values_list("action", "from_status", "to_status"))


def _snapshot(expense):
    """Tout ce qu'une transition écrirait : un refus doit le laisser strictement identique."""
    row = Expense.objects.get(pk=expense.pk)
    return {
        "status": row.status, "amount": row.amount, "label": row.label,
        "cost_center": row.cost_center_id, "receipt_required": row.receipt_required,
        "submitted_at": row.submitted_at, "validated": (row.validated_at, row.validated_by_id),
        "paid": (row.paid_at, row.paid_by_id, row.payment_reference, row.payment_method,
                 row.accounting_reference),
        "history": ExpenseStatusHistory.objects.filter(expense=row).count(),
        "audit": AuditLog.objects.filter(target_id=str(row.pk)).count(),
        "counted": _counted(row),
        "adjustments": FinancialAdjustment.objects.filter(expense=row).count(),
        "allocations": CostAllocation.objects.filter(expense=row).count(),
    }


def _open_trip(sub, requester, vehicle):
    """Course à venir (non clôturée, coût non figé) du demandeur, sur `vehicle`."""
    from apps.reservations.models import Reservation
    from apps.reservations.services import _ensure_trips

    dep = timezone.now() + timedelta(days=2)
    res = Reservation.objects.create(
        subsidiary=sub, requester=requester, created_by=requester, trip_date=dep.date(),
        departure_time=dep, estimated_return=dep + timedelta(hours=1), origin="Cocody",
        destination="Plateau", purpose="Mission", passengers=2, needs_driver=False,
        trip_type=TripType.ONE_WAY, status=ReservationStatus.APPROVED,
    )
    trip = _ensure_trips(res)[0]
    Trip.objects.filter(pk=trip.pk).update(vehicle=vehicle)
    trip.refresh_from_db()
    return trip


# --- 1. Circuit complet -------------------------------------------------------------------


def test_1_full_circuit_through_the_api_counts_only_from_validated(api, sub_a, fleet_a, fin_a, fin_a2):
    """1. BROUILLON → SOUMISE → À VALIDER → VALIDÉE → PAYÉE : chaque acteur à sa place, et la
    dépense ne pèse dans les coûts qu'une fois VALIDÉE (puis PAYÉE), jamais avant."""
    _threshold(Decimal("10000"))  # justificatif exigé : la soumission s'arrête à SOUMISE
    expense = _create(api, fleet_a, amount="15000")
    assert expense.status == "draft" and expense.created_by_id == fleet_a.pk
    assert expense.submitted_at is None and not _counted(expense)

    response = _act(api, fleet_a, expense, "submit", comment="Péage de la mission Bouaké")
    assert response.status_code == 200, response.content
    assert response.json()["status"] == "submitted" and response.json()["receipt_missing"] is True
    expense.refresh_from_db()
    assert expense.status == "submitted" and expense.submitted_at is not None
    assert not _counted(expense), "une dépense soumise n'a encore rien coûté"

    assert _upload(api, fleet_a, expense).status_code == 201
    response = _act(api, fleet_a, expense, "send-for-validation", comment="Ticket joint")
    assert response.status_code == 200, response.content
    assert response.json()["status"] == "to_validate"
    assert not _counted(expense), "une dépense à valider n'a encore rien coûté"

    response = _act(api, fin_a, expense, "validate", comment="Conforme")
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["status"] == "validated" and body["validated_by"] == str(fin_a.pk)
    assert body["is_countable"] is True
    expense.refresh_from_db()
    assert expense.validated_by_id == fin_a.pk and expense.validated_at >= expense.submitted_at
    assert (expense.paid_at, expense.paid_by_id, expense.payment_reference) == (None, None, "")
    assert _counted(expense)
    total = Expense.objects.countable().filter(subsidiary=sub_a).aggregate(s=Sum("amount"))["s"]
    assert total == Decimal("15000.00")

    response = _act(api, fin_a2, expense, "pay", payment_reference="  VIR-2026-0042 ",
                    payment_method="transfer", accounting_reference="SAP-4500012")
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["status"] == "paid" and body["paid_by"] == str(fin_a2.pk)
    expense.refresh_from_db()
    assert expense.paid_by_id == fin_a2.pk and expense.paid_at >= expense.validated_at
    assert expense.validated_by_id == fin_a.pk, "le paiement ne réécrit pas le valideur"
    assert (expense.payment_reference, expense.payment_method, expense.accounting_reference) == \
        ("VIR-2026-0042", "transfer", "SAP-4500012")
    # Payée : toujours comptée, et UNE seule fois.
    total = Expense.objects.countable().filter(subsidiary=sub_a).aggregate(s=Sum("amount"))["s"]
    assert total == Decimal("15000.00")
    assert _history(expense) == [
        ("create", "", "draft"), ("submit", "draft", "submitted"),
        ("send_for_validation", "submitted", "to_validate"),
        ("validate", "to_validate", "validated"), ("pay", "validated", "paid"),
    ]


def test_1b_submit_goes_straight_to_validation_and_the_validator_may_pay(api, sub_a, fleet_a, fin_a):
    """1. Sans justificatif exigé, la soumission transmet d'elle-même à la validation (en
    passant par SOUMISE, tracé) ; le valideur, qui n'est pas l'auteur, peut aussi payer."""
    expense = _create(api, fleet_a, amount="8000")
    response = _act(api, fleet_a, expense, "submit")
    assert response.status_code == 200 and response.json()["status"] == "to_validate"
    assert _history(expense) == [("create", "", "draft"), ("submit", "draft", "submitted"),
                                 ("send_for_validation", "submitted", "to_validate")]
    assert _act(api, fin_a, expense, "validate").status_code == 200
    response = _act(api, fin_a, expense, "pay", **PAY)
    assert response.status_code == 200, response.content
    expense.refresh_from_db()
    assert expense.status == "paid" and expense.paid_by_id == expense.validated_by_id == fin_a.pk


# --- 2. Transitions interdites, séparation des tâches, rôles -------------------------------


FORBIDDEN = [
    ("draft", "validate", {"comment": "x"}),
    ("draft", "pay", PAY),
    ("draft", "send-for-validation", {}),
    ("draft", "cancel", {"reason": "x"}),
    ("submitted", "validate", {}),
    ("submitted", "pay", PAY),
    ("submitted", "request-info", {"comment": "Complément"}),
    ("submitted", "cancel", {"reason": "x"}),
    ("to_validate", "submit", {}),
    ("to_validate", "pay", PAY),
    ("to_validate", "cancel", {"reason": "x"}),
    ("validated", "reject", {"reason": "Trop tard"}),
    ("validated", "validate", {}),
    ("validated", "submit", {}),
    ("validated", "request-info", {"comment": "x"}),
    ("paid", "cancel", {"reason": "Erreur"}),
    ("paid", "pay", PAY),
    ("paid", "reject", {"reason": "x"}),
    ("rejected", "validate", {}),
    ("rejected", "submit", {}),
    ("cancelled", "validate", {}),
    ("cancelled", "pay", PAY),
]


@pytest.mark.parametrize("status,action,data", FORBIDDEN,
                         ids=[f"{s}-{a}" for s, a, _ in FORBIDDEN])
def test_2_forbidden_transitions_are_refused_and_write_nothing(api, sub_a, fleet_a, fin_a,
                                                               status, action, data):
    """2. Hors du graphe du circuit, une action est refusée (400 « invalid_transition ») par un
    acteur qui en a pourtant le DROIT — et rien n'est écrit : statut, dates, historique, audit,
    ajustement, répartition."""
    expense = _expense(sub_a, fleet_a, status)
    before = _snapshot(expense)
    response = _act(api, fin_a, expense, action, **data)
    assert response.status_code == 400, response.content
    assert response.json()["code"] == "invalid_transition"
    assert _snapshot(expense) == before


def test_2b_status_is_read_only_and_amount_is_frozen_after_validation(api, sub_a, fleet_a, fin_a):
    """2. Le statut ne s'écrit jamais par PATCH (il change par les actions tracées) ; à valider,
    seul le centre de coût se corrige ; validée ou payée, plus rien ne se modifie."""
    api.force_authenticate(fleet_a)
    draft = _expense(sub_a, fleet_a, "draft")
    response = api.patch(f"/api/expenses/{draft.pk}/", {
        "status": "paid", "validated_by": str(fin_a.pk), "paid_by": str(fin_a.pk),
        "payment_reference": "FAUX", "validated_at": timezone.now().isoformat(),
        "receipt_required": True}, format="json")
    assert response.status_code == 200, response.content
    draft.refresh_from_db()
    assert draft.status == "draft" and draft.validated_by_id is None and draft.paid_by_id is None
    assert (draft.payment_reference, draft.validated_at, draft.receipt_required) == ("", None, False)
    assert not _counted(draft) and _history(draft) == []

    pending = _expense(sub_a, fleet_a, "to_validate")
    before = _snapshot(pending)
    response = api.patch(f"/api/expenses/{pending.pk}/", {"amount": "1"}, format="json")
    assert response.status_code == 400, response.content
    # Ignoré (200) ou refusé (400) : jamais appliqué — seules les actions changent le statut.
    response = api.patch(f"/api/expenses/{pending.pk}/", {"status": "validated"}, format="json")
    assert response.status_code in (200, 400), response.content
    assert _snapshot(pending) == before

    for status in ("validated", "paid"):
        frozen = _expense(sub_a, fleet_a, status)
        before = _snapshot(frozen)
        for change in ({"amount": "1"}, {"label": "Réécrit"}, {"date": "2026-09-01"},
                       {"status": "draft"}):
            response = api.patch(f"/api/expenses/{frozen.pk}/", change, format="json")
            assert response.status_code == 400, (status, change, response.content)
        assert _snapshot(frozen) == before
        assert Expense.objects.get(pk=frozen.pk).amount == Decimal("15000.00")


def test_2c_author_can_neither_validate_nor_pay_his_own_expense(api, sub_a, fin_a, fin_a2):
    """2. Séparation des tâches : la Finance qui SAISIT une dépense ne la valide pas et ne la
    paie pas (403 « segregation », rien d'écrit) ; un autre financier le peut."""
    expense = _create(api, fin_a, amount="20000")
    assert _act(api, fin_a, expense, "submit").json()["status"] == "to_validate"

    before = _snapshot(expense)
    response = _act(api, fin_a, expense, "validate", comment="Je me valide")
    assert response.status_code == 403, response.content
    assert response.json()["code"] == "segregation"
    assert _snapshot(expense) == before and not _counted(expense)

    assert _act(api, fin_a2, expense, "validate").status_code == 200
    before = _snapshot(expense)
    response = _act(api, fin_a, expense, "pay", **PAY)
    assert response.status_code == 403, response.content
    assert response.json()["code"] == "segregation"
    assert _snapshot(expense) == before
    assert Expense.objects.get(pk=expense.pk).paid_by_id is None

    response = _act(api, fin_a2, expense, "pay", **PAY)
    assert response.status_code == 200, response.content
    assert Expense.objects.get(pk=expense.pk).paid_by_id == fin_a2.pk


def test_2d_fleet_manager_subsidiary_admin_and_auditor_cannot_validate_pay_or_cancel(
        api, sub_a, fleet_a, fleet_a2, admin_a, auditor, fin_a):
    """2. Saisir n'est pas valider : gestionnaire de flotte et admin de filiale ne valident,
    ne rejettent, ne paient ni n'annulent ; l'auditeur n'écrit jamais. Auteur neutre
    (`fleet_a2`) : seule la barrière de rôle peut expliquer le refus."""
    pending = _expense(sub_a, fleet_a2, "to_validate")
    validated = _expense(sub_a, fleet_a2, "validated")
    attempts = [
        (pending, "validate", {}), (pending, "reject", {"reason": "x"}),
        (pending, "request-info", {"comment": "x"}),
        (validated, "pay", PAY), (validated, "cancel", {"reason": "x"}),
    ]
    snapshots = {e.pk: _snapshot(e) for e in (pending, validated)}
    for actor in (fleet_a, admin_a, auditor):
        for expense, action, data in attempts:
            response = _act(api, actor, expense, action, **data)
            assert response.status_code == 403, (actor.role, action, response.content)
    assert {pk: _snapshot(Expense(pk=pk)) for pk in snapshots} == snapshots
    # Témoin : la même action par la Finance passe — le refus tenait bien au rôle.
    assert _act(api, fin_a, pending, "validate").status_code == 200


def test_2e_company_admin_validates_and_cancels_but_never_pays(api, sub_a, fleet_a, company_admin):
    """2. L'admin entreprise valide et annule, il ne paie pas (la validation n'est pas le
    paiement)."""
    pending = _expense(sub_a, fleet_a, "to_validate")
    assert _act(api, company_admin, pending, "validate").status_code == 200
    before = _snapshot(pending)
    response = _act(api, company_admin, pending, "pay", **PAY)
    assert response.status_code == 403, response.content
    assert _snapshot(pending) == before and before["status"] == "validated"
    response = _act(api, company_admin, pending, "cancel", reason="Doublon de saisie")
    assert response.status_code == 200, response.content
    assert Expense.objects.get(pk=pending.pk).status == "cancelled"


def test_2f_subsidiary_finance_cannot_close_configure_or_price(api, fin_a, group_finance):
    """2. La Finance de FILIALE ne clôture pas un mois, ne règle pas le seuil de justificatif
    et ne touche pas au barème — gestes de niveau groupe. La Finance GROUPE, elle, le peut."""
    api.force_authenticate(fin_a)
    assert api.get("/api/finance/settings/").status_code == 200  # lecture : oui
    response = api.put("/api/finance/settings/", {"receipt_required_from": "0"}, format="json")
    assert response.status_code == 403, response.content
    assert FinanceSettings.current().receipt_required_from is None
    response = api.post("/api/finance/periods/", {"period": "2025-03"}, format="json")
    assert response.status_code == 403, response.content
    assert not FinancialPeriod.objects.filter(year=2025, month=3, status=FinancialPeriod.CLOSED).exists()
    rules = TripPricingRule.objects.count()
    response = api.post("/api/finance/trip-pricing-rules/", {
        "name": "Hausse", "amount_per_km": "999", "valid_from": "2026-11-01", "reason": "x"},
        format="json")
    assert response.status_code == 403, response.content
    assert TripPricingRule.objects.count() == rules

    api.force_authenticate(group_finance)
    response = api.put("/api/finance/settings/", {"receipt_required_from": "50000"}, format="json")
    assert response.status_code == 200, response.content
    assert FinanceSettings.current().receipt_required_from == Decimal("50000.00")
    response = api.post("/api/finance/periods/", {"period": "2025-03"}, format="json")
    assert response.status_code == 200, response.content
    assert FinancialPeriod.objects.get(year=2025, month=3).status == FinancialPeriod.CLOSED


# --- 4. Justificatif obligatoire --------------------------------------------------------


def test_4a_threshold_holds_the_expense_until_a_receipt_is_uploaded(api, sub_a, fleet_a, fin_a):
    """4. Seuil 50 000 : une dépense de 50 000 reste SOUMISE, ne passe pas à la validation sans
    pièce ; un fichier refusé ne compte pas ; une pièce déposée par l'API débloque."""
    _threshold(Decimal("50000"))
    expense = _create(api, fleet_a, amount="50000")
    response = _act(api, fleet_a, expense, "submit")
    assert response.status_code == 200 and response.json()["status"] == "submitted"
    assert response.json()["receipt_missing"] is True

    before = _snapshot(expense)
    response = _act(api, fleet_a, expense, "send-for-validation")
    assert response.status_code == 400 and response.json()["code"] == "receipt_required"
    assert _snapshot(expense) == before

    # Un fichier hors formats admis n'est pas un justificatif.
    api.force_authenticate(fleet_a)
    bad = SimpleUploadedFile("ticket.txt", b"pas une piece", content_type="text/plain")
    response = api.post(f"/api/expenses/{expense.pk}/attachments/", {"file": bad, "kind": "receipt"},
                        format="multipart")
    assert response.status_code == 400 and expense.attachments.count() == 0
    assert _act(api, fleet_a, expense, "send-for-validation").json()["code"] == "receipt_required"

    response = _upload(api, fleet_a, expense)
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["receipt_missing"] is False and len(body["attachments"]) == 1
    url = body["attachments"][0]["url"]
    assert "/api/files/" in url and "/media/" not in url, "pièce servie par URL signée seulement"

    response = _act(api, fleet_a, expense, "send-for-validation")
    assert response.status_code == 200 and response.json()["status"] == "to_validate"
    response = _act(api, fin_a, expense, "validate")
    assert response.status_code == 200 and response.json()["status"] == "validated"


def test_4b_validate_itself_refuses_a_missing_receipt(api, sub_a, fleet_a, fin_a, group_finance):
    """4. Le seuil vaut aussi à la VALIDATION : une dépense déjà « à valider » quand la Finance
    groupe abaisse le seuil ne se valide plus sans pièce (400 « receipt_required », rien
    d'écrit, pas comptée)."""
    expense = _create(api, fleet_a, amount="80000")
    assert _act(api, fleet_a, expense, "submit").json()["status"] == "to_validate"
    api.force_authenticate(group_finance)
    assert api.put("/api/finance/settings/", {"receipt_required_from": "50000"},
                   format="json").status_code == 200

    before = _snapshot(expense)
    response = _act(api, fin_a, expense, "validate")
    assert response.status_code == 400 and response.json()["code"] == "receipt_required"
    assert _snapshot(expense) == before and not before["counted"]

    assert _upload(api, fleet_a, expense, name="facture.png", kind="invoice").status_code == 201
    response = _act(api, fin_a, expense, "validate")
    assert response.status_code == 200 and response.json()["status"] == "validated"
    assert _counted(expense)


def test_4c_just_under_the_threshold_needs_no_receipt(api, sub_a, fleet_a, fin_a):
    """4. 49 999,99 sous un seuil de 50 000 : aucune pièce exigée, circuit complet sans pièce."""
    _threshold(Decimal("50000"))
    expense = _create(api, fleet_a, amount="49999.99")
    response = _act(api, fleet_a, expense, "submit")
    assert response.json()["status"] == "to_validate" and response.json()["receipt_missing"] is False
    assert _act(api, fin_a, expense, "validate").status_code == 200
    expense.refresh_from_db()
    assert expense.status == "validated" and expense.attachments.count() == 0


def test_4d_zero_threshold_always_requires_a_receipt(api, sub_a, fleet_a):
    """4. Seuil 0 : même une dépense d'un franc exige sa pièce."""
    _threshold(Decimal("0"))
    expense = _create(api, fleet_a, amount="1")
    assert _act(api, fleet_a, expense, "submit").json()["status"] == "submitted"
    response = _act(api, fleet_a, expense, "send-for-validation")
    assert response.status_code == 400 and response.json()["code"] == "receipt_required"
    assert Expense.objects.get(pk=expense.pk).status == "submitted"


def test_4e_empty_threshold_never_requires_a_receipt_automatically(api, sub_a, fleet_a, fin_a):
    """4. Seuil vide : aucune obligation automatique, quel que soit le montant."""
    _threshold(None)
    expense = _create(api, fleet_a, amount="5000000")
    assert _act(api, fleet_a, expense, "submit").json()["status"] == "to_validate"
    assert _act(api, fin_a, expense, "validate").status_code == 200
    assert Expense.objects.get(pk=expense.pk).status == "validated"


def test_4f_finance_can_require_a_receipt_under_the_threshold(api, sub_a, fleet_a, fin_a):
    """4. « Demander un complément » avec exigence de pièce : la dépense, sous le seuil, ne
    repart plus à la validation sans justificatif."""
    _threshold(Decimal("50000"))
    expense = _create(api, fleet_a, amount="1000")
    assert _act(api, fleet_a, expense, "submit").json()["status"] == "to_validate"

    response = _act(api, fin_a, expense, "request-info", comment="Joignez le ticket de péage",
                    require_receipt=True)
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["status"] == "submitted" and body["receipt_required"] is True
    assert body["receipt_missing"] is True
    response = _act(api, fleet_a, expense, "send-for-validation")
    assert response.status_code == 400 and response.json()["code"] == "receipt_required"

    assert _upload(api, fleet_a, expense, kind="toll_ticket").status_code == 201
    assert _act(api, fleet_a, expense, "send-for-validation").json()["status"] == "to_validate"
    assert _act(api, fin_a, expense, "validate").json()["status"] == "validated"
    row = ExpenseStatusHistory.objects.get(expense=expense, action="request_info")
    assert row.details == {"require_receipt": True} and row.user_id == fin_a.pk


# --- 6. Traçabilité --------------------------------------------------------------------


def test_6_every_transition_is_traced_with_actor_amount_and_cost_center(api, sub_a, fleet_a, fin_a, fin_a2):
    """6. Une ligne d'historique par transition (qui, quand, ancien → nouveau statut,
    commentaire, montant et centre de coût AU MOMENT de l'action), une entrée d'audit
    `expense_<action>` par geste, lisibles par l'API."""
    cc_first = CostCenter.objects.create(subsidiary=sub_a, code="LOG", name="Logistique")
    cc_final = CostCenter.objects.create(subsidiary=sub_a, code="DIR", name="Direction")
    expense = _create(api, fleet_a, amount="12000", cost_center=str(cc_first.pk))
    api.force_authenticate(fleet_a)
    response = api.patch(f"/api/expenses/{expense.pk}/",
                         {"amount": "13000", "cost_center": str(cc_final.pk)}, format="json")
    assert response.status_code == 200, response.content
    assert _act(api, fleet_a, expense, "submit", comment="Note de frais mission").status_code == 200
    assert _act(api, fin_a, expense, "validate", comment="Conforme").status_code == 200
    assert _act(api, fin_a2, expense, "pay", **PAY).status_code == 200

    rows = list(ExpenseStatusHistory.objects.filter(expense=expense))
    assert [(r.action, r.from_status, r.to_status, r.user_id, r.amount, r.cost_center_id)
            for r in rows] == [
        ("create", "", "draft", fleet_a.pk, Decimal("12000.00"), cc_first.pk),
        # La correction de l'auteur est tracée (revue F2) : montant et centre de coût APRÈS.
        ("edit", "draft", "draft", fleet_a.pk, Decimal("13000.00"), cc_final.pk),
        ("submit", "draft", "submitted", fleet_a.pk, Decimal("13000.00"), cc_final.pk),
        ("send_for_validation", "submitted", "to_validate", fleet_a.pk, Decimal("13000.00"), cc_final.pk),
        ("validate", "to_validate", "validated", fin_a.pk, Decimal("13000.00"), cc_final.pk),
        ("pay", "validated", "paid", fin_a2.pk, Decimal("13000.00"), cc_final.pk),
    ]
    assert rows[1].details["diff"]["amount"] == ["12000.00", "13000.00"]
    assert rows[2].comment == "Note de frais mission" and rows[4].comment == "Conforme"
    assert rows[5].details["payment_reference"] == "VIR-2026-0042"
    assert all(r.at is not None for r in rows) and [r.at for r in rows] == sorted(r.at for r in rows)

    logs = AuditLog.objects.filter(target_id=str(expense.pk), changes__action__startswith="expense_")
    assert sorted(log.changes["action"] for log in logs) == sorted([
        "expense_create", "expense_edit", "expense_submit", "expense_send_for_validation",
        "expense_validate", "expense_pay"])
    validation = logs.get(changes__action="expense_validate")
    assert validation.actor_id == fin_a.pk and validation.action == AuditAction.UPDATE
    assert (validation.changes["from"], validation.changes["to"], validation.changes["amount"],
            validation.changes["comment"]) == ("to_validate", "validated", "13000.00", "Conforme")
    payment = logs.get(changes__action="expense_pay")
    assert payment.actor_id == fin_a2.pk and payment.changes["payment_method"] == "transfer"

    api.force_authenticate(fin_a)
    served = api.get(f"/api/expenses/{expense.pk}/history/").json()
    assert [h["action"] for h in served] == [r.action for r in rows]
    assert served[4]["user"] == fin_a.get_full_name() and served[4]["amount"] == "13000.00"
    assert served[4]["cost_center"] == str(cc_final) and served[4]["comment"] == "Conforme"


def test_6b_reject_and_cancel_reasons_are_traced(api, sub_a, fleet_a, fin_a):
    """6. Le motif d'un rejet et d'une annulation est conservé dans l'historique et l'audit."""
    rejected = _create(api, fleet_a, amount="3000")
    _act(api, fleet_a, rejected, "submit")
    assert _act(api, fin_a, rejected, "reject", reason="Doublon de la note 12").status_code == 200
    row = ExpenseStatusHistory.objects.get(expense=rejected, action="reject")
    assert (row.from_status, row.to_status, row.reason, row.user_id) == \
        ("to_validate", "rejected", "Doublon de la note 12", fin_a.pk)
    assert AuditLog.objects.get(target_id=str(rejected.pk), changes__action="expense_reject") \
        .changes["reason"] == "Doublon de la note 12"

    cancelled = _expense(sub_a, fleet_a, "validated", amount="4000")
    assert _act(api, fin_a, cancelled, "cancel", reason="Facture annulée").status_code == 200
    row = ExpenseStatusHistory.objects.get(expense=cancelled, action="cancel")
    assert (row.from_status, row.to_status, row.reason, row.amount) == \
        ("validated", "cancelled", "Facture annulée", Decimal("4000.00"))


def test_6c_history_rows_are_immutable(api, sub_a, fleet_a, fin_a):
    """6. L'historique ne se réécrit ni ne s'efface (instance, queryset, cascade depuis la
    dépense) : ce qui a été validé reste lisible."""
    expense = _create(api, fleet_a)
    _act(api, fleet_a, expense, "submit")
    _act(api, fin_a, expense, "validate", comment="Conforme")
    row = ExpenseStatusHistory.objects.get(expense=expense, action="validate")
    count = ExpenseStatusHistory.objects.filter(expense=expense).count()
    assert count == 4

    row.comment, row.amount = "Réécrit", Decimal("1")
    with pytest.raises(ProtectedError), transaction.atomic():
        row.save()
    with pytest.raises(ProtectedError), transaction.atomic():
        ExpenseStatusHistory.objects.get(pk=row.pk).delete()
    with pytest.raises(ProtectedError), transaction.atomic():
        ExpenseStatusHistory.objects.filter(expense=expense).delete()
    with pytest.raises(ProtectedError), transaction.atomic():
        Expense.objects.get(pk=expense.pk).delete()

    kept = ExpenseStatusHistory.objects.get(pk=row.pk)
    assert (kept.comment, kept.amount) == ("Conforme", Decimal("15000.00"))
    assert ExpenseStatusHistory.objects.filter(expense=expense).count() == count
    assert Expense.objects.filter(pk=expense.pk).exists()


# --- 15. Centre de coût --------------------------------------------------------------------


def test_15_cost_center_is_prefilled_editable_in_the_circuit_and_traced(
        api, sub_a, requester_a, fleet_a, fin_a, vehicle_a):
    """15. Course → demandeur → service → centre de coût ACTIF : suggéré par l'API, prérempli à
    la création sans centre, modifiable en brouillon et soumise (à valider : `test_15c`), figé
    après validation, et la valeur finale est celle tracée par la validation."""
    department = Department.objects.create(subsidiary=sub_a, name="Logistique")
    requester_a.department = department
    requester_a.save(update_fields=["department"])
    # Inactif et trié en premier : s'il était proposé, le filtre « actif » aurait sauté.
    CostCenter.objects.create(subsidiary=sub_a, code="A-OLD", name="Ancien", department=department,
                              active=False)
    center = CostCenter.objects.create(subsidiary=sub_a, code="LOG", name="Logistique",
                                       kind="department", department=department)
    other = CostCenter.objects.create(subsidiary=sub_a, code="DIR", name="Direction")
    trip = _open_trip(sub_a, requester_a, vehicle_a)

    api.force_authenticate(fleet_a)
    suggestion = api.get(f"/api/expenses/cost-center-suggestion/?trip={trip.pk}").json()
    assert suggestion == {"cost_center": str(center.pk), "label": "LOG — Logistique"}

    expense = _create(api, fleet_a, trip=str(trip.pk))
    assert expense.cost_center_id == center.pk and expense.subsidiary_id == sub_a.pk
    assert ExpenseStatusHistory.objects.get(expense=expense, action="create").cost_center_id == center.pk
    explicit = _create(api, fleet_a, trip=str(trip.pk), cost_center=str(other.pk))
    assert explicit.cost_center_id == other.pk, "un centre saisi n'est pas écrasé par la suggestion"

    _threshold(Decimal("0"))  # pièce exigée : la soumission s'arrête à SOUMISE
    api.force_authenticate(fleet_a)
    url = f"/api/expenses/{expense.pk}/"
    response = api.patch(url, {"cost_center": str(other.pk)}, format="json")  # brouillon
    assert response.status_code == 200, response.content
    assert Expense.objects.get(pk=expense.pk).cost_center_id == other.pk
    assert _act(api, fleet_a, expense, "submit").json()["status"] == "submitted"
    assert ExpenseStatusHistory.objects.get(expense=expense, action="submit").cost_center_id == other.pk
    api.force_authenticate(fleet_a)
    response = api.patch(url, {"cost_center": str(center.pk)}, format="json")  # soumise
    assert response.status_code == 200, response.content
    assert _upload(api, fleet_a, expense).status_code == 201
    assert _act(api, fleet_a, expense, "send-for-validation").json()["status"] == "to_validate"

    assert _act(api, fin_a, expense, "validate").status_code == 200
    # La validation trace la valeur FINALE, pas celle de la soumission.
    assert ExpenseStatusHistory.objects.get(expense=expense, action="validate").cost_center_id == center.pk
    api.force_authenticate(fleet_a)
    assert api.patch(url, {"cost_center": str(other.pk)}, format="json").status_code == 400
    assert Expense.objects.get(pk=expense.pk).cost_center_id == center.pk


def test_15c_cost_center_alone_stays_editable_while_to_validate(api, sub_a, fleet_a, fin_a):
    """15. À VALIDER, le centre de coût (et lui seul) se corrige encore — le valideur trace la
    valeur corrigée ; un montant glissé avec lui est refusé sans rien écrire."""
    first = CostCenter.objects.create(subsidiary=sub_a, code="LOG", name="Logistique")
    final = CostCenter.objects.create(subsidiary=sub_a, code="DIR", name="Direction")
    expense = _create(api, fleet_a, cost_center=str(first.pk))
    assert _act(api, fleet_a, expense, "submit").json()["status"] == "to_validate"

    api.force_authenticate(fleet_a)
    url = f"/api/expenses/{expense.pk}/"
    before = _snapshot(expense)
    response = api.patch(url, {"cost_center": str(final.pk), "amount": "1"}, format="json")
    assert response.status_code == 400, response.content
    assert _snapshot(expense) == before

    response = api.patch(url, {"cost_center": str(final.pk)}, format="json")
    assert response.status_code == 200, response.content
    assert response.json()["status"] == "to_validate" and response.json()["cost_center"] == str(final.pk)
    assert Expense.objects.get(pk=expense.pk).amount == Decimal("15000.00")
    assert _act(api, fin_a, expense, "validate").status_code == 200
    assert ExpenseStatusHistory.objects.get(expense=expense, action="validate").cost_center_id == final.pk


def test_15b_cost_center_of_another_subsidiary_is_refused_and_never_suggested(
        api, sub_a, sub_b, fleet_a, vehicle_a):
    """15. Un centre de coût d'une filiale sœur (ou inactif) est refusé à la création comme à
    la modification, sans rien écrire ; la suggestion ne révèle pas celui d'une course d'une
    autre filiale."""
    center_b = CostCenter.objects.create(subsidiary=sub_b, code="DKR-1", name="Dakar")
    inactive = CostCenter.objects.create(subsidiary=sub_a, code="OLD", name="Fermé", active=False)
    api.force_authenticate(fleet_a)
    count = Expense.objects.count()
    for refused in (center_b, inactive):
        response = api.post("/api/expenses/", {
            "category": "toll", "label": "Péage", "amount": "1000", "date": _today().isoformat(),
            "cost_center": str(refused.pk)}, format="json")
        assert response.status_code == 400 and "cost_center" in response.json(), response.content
    assert Expense.objects.count() == count

    expense = _create(api, fleet_a)
    for refused in (center_b, inactive):
        api.force_authenticate(fleet_a)
        response = api.patch(f"/api/expenses/{expense.pk}/", {"cost_center": str(refused.pk)},
                             format="json")
        assert response.status_code == 400 and "cost_center" in response.json()
    assert Expense.objects.get(pk=expense.pk).cost_center_id is None

    department_b = Department.objects.create(subsidiary=sub_b, name="Achats")
    CostCenter.objects.filter(pk=center_b.pk).update(department=department_b)
    requester_b = _user("req-b-wf@test.io", RoleChoices.REQUESTER, sub_b)
    requester_b.department = department_b
    requester_b.save(update_fields=["department"])
    vehicle_b = Vehicle.objects.create(subsidiary=sub_b, registration="B-WF", brand="R", model="Y")
    trip_b = _open_trip(sub_b, requester_b, vehicle_b)
    api.force_authenticate(fleet_a)
    assert api.get(f"/api/expenses/cost-center-suggestion/?trip={trip_b.pk}").json() == \
        {"cost_center": None, "label": None}


# --- Compléments du circuit ---------------------------------------------------------------


def test_request_info_sends_back_to_submitted_with_its_comment(api, sub_a, fleet_a, fin_a):
    """« Demander un complément » : retour à SOUMISE avec le commentaire (obligatoire), sans
    exiger de pièce si on ne l'a pas demandé ; l'auteur corrige et renvoie."""
    expense = _create(api, fleet_a)
    _act(api, fleet_a, expense, "submit")
    before = _snapshot(expense)
    for empty in ("", "   "):
        response = _act(api, fin_a, expense, "request-info", comment=empty)
        assert response.status_code == 400 and response.json()["code"] == "comment_required"
    assert _snapshot(expense) == before

    response = _act(api, fin_a, expense, "request-info", comment="Précisez le trajet",
                    require_receipt=False)
    assert response.status_code == 200, response.content
    assert response.json()["status"] == "submitted" and response.json()["receipt_required"] is False
    row = ExpenseStatusHistory.objects.filter(expense=expense).last()
    assert (row.action, row.from_status, row.to_status, row.comment, row.user_id) == \
        ("request_info", "to_validate", "submitted", "Précisez le trajet", fin_a.pk)

    api.force_authenticate(fleet_a)
    response = api.patch(f"/api/expenses/{expense.pk}/", {"label": "Péage Abidjan → Yamoussoukro"},
                         format="json")
    assert response.status_code == 200
    assert _act(api, fleet_a, expense, "send-for-validation").json()["status"] == "to_validate"


def test_reject_requires_a_reason_and_a_rejected_expense_is_never_counted(api, sub_a, fleet_a, fin_a):
    """Un rejet sans motif est refusé (rien d'écrit) ; rejetée — depuis À VALIDER comme depuis
    SOUMISE — la dépense n'est jamais comptée et ne revient pas dans le circuit."""
    expense = _create(api, fleet_a)
    _act(api, fleet_a, expense, "submit")
    before = _snapshot(expense)
    for empty in ({}, {"reason": "  "}):
        response = _act(api, fin_a, expense, "reject", **empty)
        assert response.status_code == 400 and response.json()["code"] == "reason_required"
    assert _snapshot(expense) == before

    assert _act(api, fin_a, expense, "reject", reason="Hors politique de frais").status_code == 200
    assert Expense.objects.get(pk=expense.pk).status == "rejected" and not _counted(expense)
    assert _act(api, fleet_a, expense, "submit").status_code == 400

    _threshold(Decimal("0"))
    held = _create(api, fleet_a, amount="2000")
    assert _act(api, fleet_a, held, "submit").json()["status"] == "submitted"
    assert _act(api, fin_a, held, "reject", reason="Sans objet").json()["status"] == "rejected"


def test_cancel_of_a_validated_expense_in_an_open_month_removes_it_from_costs(api, sub_a, fleet_a, fin_a):
    """Annuler une dépense validée d'un mois ouvert (motif obligatoire) la retire des coûts."""
    expense = _create(api, fleet_a, amount="6500")
    _act(api, fleet_a, expense, "submit")
    _act(api, fin_a, expense, "validate")
    assert _counted(expense)

    before = _snapshot(expense)
    response = _act(api, fin_a, expense, "cancel")
    assert response.status_code == 400 and response.json()["code"] == "reason_required"
    assert _snapshot(expense) == before

    response = _act(api, fin_a, expense, "cancel", reason="Facture annulée par le fournisseur")
    assert response.status_code == 200, response.content
    assert Expense.objects.get(pk=expense.pk).status == "cancelled"
    assert not _counted(expense)
    assert Expense.objects.countable().filter(subsidiary=sub_a).aggregate(s=Sum("amount"))["s"] is None


def test_cancel_in_a_closed_month_is_refused_with_a_negative_adjustment_proposal(
        api, sub_a, fleet_a, fin_a, company_admin):
    """Une dépense validée d'un mois CLOS ne s'annule pas (409) : on propose un ajustement
    négatif sur la période ouverte ; rien n'est écrit, elle reste comptée dans son mois."""
    expense = _expense(sub_a, fleet_a, "validated", amount="7000", day=date(2025, 3, 11))
    close_period(2025, 3, company_admin)
    before = _snapshot(expense)
    response = _act(api, fin_a, expense, "cancel", reason="Erreur de saisie")
    assert response.status_code == 409, response.content
    body = response.json()
    today = _today()
    assert body["code"] == "adjustment_required"
    assert (body["adjustment_proposal"]["amount"], body["adjustment_proposal"]["original_period"],
            body["adjustment_proposal"]["posting_period"]) == \
        ("-7000.00", "2025-03", f"{today.year}-{today.month:02d}")
    assert _snapshot(expense) == before and before["counted"]
    assert FinancialAdjustment.objects.count() == 0


def test_delete_discards_a_draft_but_keeps_the_row_and_its_trace(api, sub_a, fleet_a):
    """DELETE d'un brouillon = abandon : la ligne reste (« annulée »), l'abandon est tracé, et
    la dépense ne revient pas dans le circuit."""
    expense = _create(api, fleet_a)
    api.force_authenticate(fleet_a)
    response = api.delete(f"/api/expenses/{expense.pk}/")
    assert response.status_code == 204, response.content
    expense.refresh_from_db()
    assert expense.status == "cancelled" and not _counted(expense)
    assert _history(expense) == [("create", "", "draft"), ("discard", "draft", "cancelled")]
    row = ExpenseStatusHistory.objects.get(expense=expense, action="discard")
    assert row.user_id == fleet_a.pk and row.reason
    assert AuditLog.objects.filter(target_id=str(expense.pk), changes__action="expense_discard").count() == 1
    assert _act(api, fleet_a, expense, "submit").status_code == 400
    api.force_authenticate(fleet_a)
    assert api.delete(f"/api/expenses/{expense.pk}/").status_code == 400


@pytest.mark.parametrize("status", ["submitted", "to_validate", "validated", "paid", "rejected"])
def test_delete_of_an_expense_in_the_circuit_is_refused(api, sub_a, fleet_a, status):
    """Une dépense entrée dans le circuit ne se supprime pas (on la rejette ou l'annule) :
    DELETE refusé, ligne, statut et historique intacts."""
    expense = _expense(sub_a, fleet_a, status)
    before = _snapshot(expense)
    api.force_authenticate(fleet_a)
    response = api.delete(f"/api/expenses/{expense.pk}/")
    assert response.status_code == 400, response.content
    assert _snapshot(expense) == before
