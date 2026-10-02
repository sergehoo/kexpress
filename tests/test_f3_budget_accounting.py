"""F3 — Comptabilité budgétaire : engagé, réalisé, décaissé — chaque montant compté une fois.

Invariants couverts (numérotation de ce module), chacun par des tests qui échoueraient si on
retirait la règle qu'ils gardent :

1. engagé, réalisé et décaissé sont DISTINCTS et ne s'additionnent jamais : un brouillon ne
   compte nulle part ; soumise / à valider → engagé seulement ; validée → réalisé seulement
   (l'engagé retombe) ; payée → toujours réalisée, et décaissée au mois du PAIEMENT (pas à
   celui de la dépense) ; rejetée / annulée → plus rien ;
2. aucun double comptage : la pièce d'un plein ou d'une maintenance compte une fois, par sa
   source (énergie / maintenance), jamais dans sa catégorie de dépense ; une reprise legacy
   n'est pas comptée ; une dépense tardive portée par un ajustement approuvé compte une
   fois, au mois de COMPTABILISATION de l'ajustement (pas dans son mois d'origine clos) ;
   un ajustement à approuver est engagé ; énergie = pleins + recharges ; maintenance
   terminée → réalisé, planifiée chiffrée → engagé ; assurance proratisée au jour
   (365 000 sur 2025 = 31 000 en mars) ; charges véhicule et visites proratisées en charges
   fixes ; le barème kilométrique (`TripPricing`) n'apparaît jamais ;
3. une dépense de MISSION se répartit entre les filiales de ses courses : validée, chaque
   filiale porte la part `CostAllocation` de sa course (Σ = 1001.00 au centime) ; engagée,
   elle est répartie provisoirement avec la même clé ; une ligne du budget de B ne voit que
   la part de B ;
4. un mois CLOS est figé : à la clôture, `BudgetActual` = cellules calculées à cet instant ;
   valider ensuite une dépense de ce mois ne change pas `month_cells` (elle passe par un
   ajustement sur le mois de comptabilisation) ; les lignes figées ne se modifient ni ne
   se suppriment ;
5. chiffres d'une ligne : prévu, engagé, réalisé, décaissé, disponible = prévu − réalisé −
   engagé, taux ; axes jokers (ligne annuelle, toutes catégories, tous centres de coût),
   centre de coût en correspondance exacte ; les totaux du tableau de bord = somme des
   lignes ; une ligne qui en chevauche une autre est refusée (un total ne compte jamais
   deux fois) ;
6. révisions : en brouillon, historisées « draft » ; approuvé (par un autre que l'auteur),
   motif obligatoire ; une ligne mensuelle d'un mois clos ne se révise ni ne s'ajoute plus ;
   l'historique est immuable ; une ligne ne se supprime qu'en brouillon.

Mois de référence : 2025 (mars clos par les tests qui en ont besoin, via `close_period`). Le
mois de comptabilisation d'un ajustement et celui d'un paiement passé par le circuit est le
mois en cours, CALCULÉ (jamais codé en dur) ; les autres paiements ont une date explicite.
"""
from datetime import date, datetime, time
from decimal import Decimal

import pytest
from django.db import transaction
from django.db.models import ProtectedError
from django.utils import timezone

from apps.core.enums import RoleChoices
from apps.expenses import workflow
from apps.expenses.models import ElectricCharge, Expense, FuelLog
from apps.finance.adjustments import approve as approve_adjustment
from apps.finance.adjustments import create_adjustment
from apps.finance.adjustments import reject as reject_adjustment
from apps.finance.budget import (
    BudgetError, add_line, approve, create_budget, remove_line, revise_line,
)
from apps.finance.budget_read import cells_for_year, dashboard, line_figures, live_cells, month_cells
from apps.finance.costing import allocate
from apps.finance.locks import FinancialHistoryLocked
from apps.finance.models import (
    Budget, BudgetActual, BudgetLine, BudgetRevision, CostAllocation, CostCenter,
    FinancialAdjustment, TripPricing, TripPricingRule, VehicleCharge,
)
from apps.finance.periods import close_period
from apps.finance.trip_cost import mission_weights
from apps.maintenance.models import MaintenanceRecord, MaintenanceType
from apps.vehicles.models import InsurancePolicy, TechnicalInspection, Vehicle, VehicleRevision
from tests.test_finance_f1 import _closed_trip, _mission, _user

pytestmark = pytest.mark.django_db

D = Decimal
MARCH = (2025, 3)


# --- Outillage ----------------------------------------------------------------------


@pytest.fixture
def vehicle(sub_a):
    return Vehicle.objects.create(subsidiary=sub_a, registration="F3A-A", brand="Toyota", model="Hilux")


@pytest.fixture
def finance_a(sub_a):
    """Financier de la filiale A : valide, paie, annule, gère le budget de A."""
    return _user("fin-a-f3a@test.io", RoleChoices.FINANCE, sub_a)


@pytest.fixture
def group_finance(db):
    """Financier groupe (sans filiale) : approuve les budgets."""
    return _user("fin-groupe-f3a@test.io", RoleChoices.FINANCE)


def _now():
    """Mois en cours : celui où se comptabilise un ajustement et où tombe un paiement du circuit."""
    today = timezone.localdate()
    return today.year, today.month


def _paid_at(day):
    """Instant de paiement explicite (fuseau du projet)."""
    return timezone.make_aware(datetime.combine(day, time(10, 0)), timezone.get_current_timezone())


def _key(sub, category, center=None):
    return (str(sub.pk), str(center.pk) if center else None, category)


def _row(engaged="0", realised="0", disbursed="0"):
    return {"engaged": D(engaged), "realised": D(realised), "disbursed": D(disbursed)}


def _live(year, month):
    """Cellules NON NULLES d'un mois, calculées en direct sur les données."""
    return {key: {m: D(v) for m, v in values.items()}
            for key, values in live_cells(year, month).items() if any(values.values())}


def _read(year, month):
    """Cellules non nulles telles que le budget les LIT (figées si le mois est clos)."""
    return {key: {m: D(v) for m, v in values.items()}
            for key, values in month_cells(year, month).items() if any(values.values())}


def _expense(sub, author, **fields):
    """Dépense saisie par `author` (statut explicite : l'ORM crée un brouillon par défaut)."""
    fields.setdefault("label", f"{fields.get('category', 'other')} F3")
    fields.setdefault("status", "draft")
    return Expense.objects.create(subsidiary=sub, created_by=author, **fields)


def _status(expense):
    expense.refresh_from_db()
    return expense.status


# --- 1. Engagé, réalisé, décaissé : distincts, jamais additionnés ----------------------


def test_1_expense_moves_from_engaged_to_realised_and_is_disbursed_in_its_payment_month(
        sub_a, fleet_a, finance_a):
    """Invariant 1 : au fil du circuit réel, une dépense est d'abord ENGAGÉE (soumise, à
    valider), puis RÉALISÉE (validée — l'engagé retombe à 0), puis DÉCAISSÉE au mois du
    paiement (mois en cours) sans cesser d'être réalisée au mois de sa date (avril 2025) :
    le montant n'apparaît jamais deux fois dans la même mesure, ni en engagé + réalisé."""
    april, now = (2025, 4), _now()
    key = _key(sub_a, "parking")
    expense = _expense(sub_a, fleet_a, category="parking", amount="1234.56", date=date(2025, 4, 10))

    # Brouillon : n'a rien coûté, n'engage rien.
    assert _live(*april) == {} and _live(*now) == {}

    workflow.submit(expense, fleet_a)  # sans seuil de justificatif : passe « à valider »
    assert _status(expense) == "to_validate"
    assert _live(*april) == {key: _row(engaged="1234.56")}
    workflow.request_info(expense, finance_a, "Précisez le lieu de stationnement.")
    assert _status(expense) == "submitted"
    assert _live(*april) == {key: _row(engaged="1234.56")}, "une dépense soumise est engagée"
    workflow.send_for_validation(expense, fleet_a)
    workflow.validate(expense, finance_a)
    assert _status(expense) == "validated"
    assert _live(*april) == {key: _row(realised="1234.56")}, "validée : réalisé seul, l'engagé retombe"
    assert _live(*now) == {}

    workflow.pay(expense, finance_a, payment_reference="VIR-F3A-1", payment_method="transfer")
    assert _status(expense) == "paid"
    # Réalisé au mois de la DÉPENSE, décaissé au mois du PAIEMENT : deux mesures, une dépense.
    assert _live(*april) == {key: _row(realised="1234.56")}
    assert _live(*now) == {key: _row(disbursed="1234.56")}
    realised_total = sum(c["realised"] for m in (april, now) for c in _live(*m).values())
    assert realised_total == D("1234.56"), "le paiement ne s'ajoute pas au réalisé"


def test_1b_rejected_cancelled_or_discarded_expenses_count_nowhere(sub_a, fleet_a, finance_a):
    """Invariant 1 : une dépense rejetée (après avoir été engagée), annulée (après avoir été
    réalisée) ou abandonnée en brouillon ne pèse plus sur aucune mesure."""
    april = (2025, 4)
    rejected = _expense(sub_a, fleet_a, category="toll", amount="500", date=date(2025, 4, 11))
    workflow.submit(rejected, fleet_a)
    assert _live(*april) == {_key(sub_a, "toll"): _row(engaged="500")}
    workflow.reject(rejected, finance_a, "Hors politique de déplacement")
    assert _status(rejected) == "rejected"
    assert _live(*april) == {}, "une dépense rejetée resterait engagée"

    cancelled = _expense(sub_a, fleet_a, category="washing", amount="300", date=date(2025, 4, 12))
    workflow.submit(cancelled, fleet_a)
    workflow.validate(cancelled, finance_a)
    assert _live(*april) == {_key(sub_a, "washing"): _row(realised="300")}
    workflow.cancel(cancelled, finance_a, "Doublon de saisie")
    assert _status(cancelled) == "cancelled"
    assert _live(*april) == {}, "une dépense annulée resterait réalisée"

    discarded = _expense(sub_a, fleet_a, category="road_fees", amount="200", date=date(2025, 4, 13))
    workflow.discard(discarded, fleet_a)
    assert _status(discarded) == "cancelled"
    assert _live(*april) == {} and _live(*_now()) == {}


# --- 2. Aucun double comptage --------------------------------------------------------


def test_2a_pieces_of_a_source_and_legacy_expenses_are_counted_once_by_the_source(
        sub_a, fleet_a, vehicle):
    """Invariant 2 : énergie = plein + recharge ; maintenance terminée (+ révision) →
    réalisé, planifiée chiffrée → engagé ; la PIÈCE d'un plein (catégorie carburant) et
    celle d'une maintenance saisie en catégorie ordinaire (« Réparation ») ne comptent
    nulle part en dépense ; une reprise legacy, même de catégorie ordinaire (péage), non
    plus. Seul le vrai péage compte en péage."""
    kind = MaintenanceType.objects.create(name="Vidange F3A")
    fuel = FuelLog.objects.create(vehicle=vehicle, subsidiary=sub_a, date=date(2025, 3, 10),
                                  liters="40", amount="32000")
    ElectricCharge.objects.create(vehicle=vehicle, subsidiary=sub_a, date=date(2025, 3, 11),
                                  kwh_recharged="20", amount="3000")
    repair = MaintenanceRecord.objects.create(vehicle=vehicle, subsidiary=sub_a, maintenance_type=kind,
                                              status="completed", performed_date=date(2025, 3, 12),
                                              cost="15000")
    VehicleRevision.objects.create(vehicle=vehicle, date=date(2025, 3, 14), mileage_at_revision=20000,
                                   cost="2000")
    MaintenanceRecord.objects.create(vehicle=vehicle, subsidiary=sub_a, maintenance_type=kind,
                                     status="planned", scheduled_date=date(2025, 3, 28), cost="9000")
    # Pièces rattachées à leur source, validées : la source porte déjà le coût.
    _expense(sub_a, fleet_a, vehicle=vehicle, category="fuel", amount="32000", date=date(2025, 3, 10),
             source_type="fuel_log", source_id=fuel.pk, status="validated")
    _expense(sub_a, fleet_a, vehicle=vehicle, category="repair", amount="15000", date=date(2025, 3, 12),
             source_type="maintenance", source_id=repair.pk, status="validated")
    # Reprises de l'historique non traitées : exclues des coûts.
    _expense(sub_a, fleet_a, vehicle=vehicle, category="toll", amount="4000", date=date(2025, 3, 15),
             source_type="legacy", status="validated")
    _expense(sub_a, fleet_a, vehicle=vehicle, category="fuel", amount="5000", date=date(2025, 3, 15),
             source_type="legacy", status="paid", paid_at=_paid_at(date(2025, 3, 16)))
    # Une vraie dépense directe : elle prouve que l'exclusion vise la pièce, pas la catégorie.
    _expense(sub_a, fleet_a, vehicle=vehicle, category="toll", amount="700", date=date(2025, 3, 10),
             status="validated")

    assert _live(*MARCH) == {
        _key(sub_a, "energy"): _row(realised="35000"),
        _key(sub_a, "maintenance"): _row(engaged="9000", realised="17000"),
        _key(sub_a, "toll"): _row(realised="700"),
    }


def test_2b_late_expense_counts_once_in_the_posting_month_and_pending_adjustments_are_engaged(
        sub_a, fleet_a, finance_a, company_admin, vehicle):
    """Invariant 2 : une dépense datée d'un mois CLOS, validée ensuite, est portée par un
    ajustement approuvé — réalisée une fois, au mois de COMPTABILISATION (mois en cours),
    jamais dans mars (ni figé, ni recalculé) ; payée, elle est décaissée sans être recomptée.
    Un ajustement à approuver est ENGAGÉ ; approuvé, il devient réalisé ; rejeté, rien."""
    close_period(*MARCH, company_admin)
    now = _now()
    late = _expense(sub_a, fleet_a, vehicle=vehicle, category="parking", amount="1200",
                    date=date(2025, 3, 20), status="to_validate")
    workflow.validate(late, finance_a)
    late.refresh_from_db()
    carrier = late.adjustment
    assert carrier.status == FinancialAdjustment.APPROVED
    assert (carrier.posting_period.year, carrier.posting_period.month) == now
    assert (carrier.original_period.year, carrier.original_period.month) == MARCH

    assert _live(*now) == {_key(sub_a, "parking"): _row(realised="1200")}
    assert _read(*MARCH) == {}, "le mois clos n'absorbe pas la dépense tardive"
    assert _live(*MARCH) == {}, "la dépense portée serait comptée aussi comme dépense"
    workflow.pay(late, finance_a, payment_reference="VIR-F3A-2", payment_method="mobile_money")
    assert _live(*now) == {_key(sub_a, "parking"): _row(realised="1200", disbursed="1200")}

    pending = create_adjustment(author=fleet_a, original=MARCH, amount="4000",
                                reason="Facture garage de mars reçue en retard", subsidiary_id=sub_a.pk,
                                source="vehicle", source_id=vehicle.pk, vehicle=vehicle,
                                category="maintenance")
    assert pending.status == FinancialAdjustment.PENDING
    assert _live(*now)[_key(sub_a, "maintenance")] == _row(engaged="4000")
    approve_adjustment(pending, finance_a)
    assert _live(*now)[_key(sub_a, "maintenance")] == _row(realised="4000")

    refused = create_adjustment(author=fleet_a, original=MARCH, amount="800", reason="Péage de mars",
                                subsidiary_id=sub_a.pk, source="vehicle", source_id=vehicle.pk,
                                vehicle=vehicle, category="toll")
    assert _live(*now)[_key(sub_a, "toll")] == _row(engaged="800")
    reject_adjustment(refused, finance_a, "Doublon")
    assert _live(*now) == {
        _key(sub_a, "parking"): _row(realised="1200", disbursed="1200"),
        _key(sub_a, "maintenance"): _row(realised="4000"),
    }, "un ajustement rejeté resterait engagé"
    assert _read(*MARCH) == {}


def test_2c_insurance_and_fixed_charges_are_prorated_and_the_km_tariff_never_appears(
        sub_a, requester_a, vehicle):
    """Invariant 2 : assurance annuelle 365 000 → 31 000 en mars (1 000 / jour) ; charge
    véhicule 3 000 du 15/02 au 16/03 → 1 400 en février, 1 600 en mars ; visite 36 500 du
    01/03/2025 au 28/02/2026 → 3 100 en mars. La valorisation au barème kilométrique d'une
    course de mars (20 000) n'entre dans AUCUNE cellule : un budget se mesure au coût réel."""
    InsurancePolicy.objects.create(vehicle=vehicle, company="NSIA", start_date=date(2025, 1, 1),
                                   expiry_date=date(2025, 12, 31), cost="365000")
    VehicleCharge.objects.create(vehicle=vehicle, kind="tax", label="Vignette", amount="3000",
                                 period_start=date(2025, 2, 15), period_end=date(2025, 3, 16))
    TechnicalInspection.objects.create(vehicle=vehicle, last_date=date(2025, 3, 1),
                                       next_date=date(2026, 3, 1), cost="36500")
    rule = TripPricingRule.objects.create(name="2025", amount_per_km="200", valid_from=date(2025, 1, 1),
                                          reason="init")
    trip = _closed_trip(sub_a, requester_a, vehicle, km="100", freeze=False)
    TripPricing.objects.filter(trip=trip).update(rule=rule, rule_version=1, amount_per_km="200",
                                                 actual_cost="20000.00", frozen_at=timezone.now())

    assert _live(*MARCH) == {
        _key(sub_a, "insurance"): _row(realised="31000"),
        _key(sub_a, "fixed_charges"): _row(realised="4700"),  # 1 600 vignette + 3 100 visite
    }
    assert _live(2025, 2) == {
        _key(sub_a, "insurance"): _row(realised="28000"),
        _key(sub_a, "fixed_charges"): _row(realised="1400"),
    }
    year = cells_for_year(2025)
    by_category = {}
    for cells in year.values():
        for (_sub, _cc, category), values in cells.items():
            by_category[category] = by_category.get(category, D("0")) + D(values["realised"])
            assert D(values["engaged"]) == 0 and D(values["disbursed"]) == 0
    # Σ des mois = la charge (assurance entière ; vignette entière ; visite : 306 jours en 2025).
    assert by_category == {"insurance": D("365000.00"), "fixed_charges": D("33600.00")}


# --- 3. Mission mutualisée : répartie entre les filiales ------------------------------


def _provisional(mission, trips, amount):
    """Part de chaque filiale selon la clé passager-km de la mission (celle des répartitions)."""
    sub_of_trip = {str(t.pk): t.subsidiary_id for t in trips}
    shares = {sub_of_trip[trip_id]: share
              for trip_id, share in allocate(D(amount), mission_weights(mission)).items()}
    assert len(shares) == 2 and all(v > 0 for v in shares.values()), "clé de mission dégénérée"
    return shares


def test_3a_validated_mission_expense_is_split_across_the_subsidiaries_of_its_trips(
        sub_a, sub_b, requester_a, fleet_a, finance_a, company_admin, vehicle):
    """Invariant 3 : un péage de 1001.00 d'une mission A + B, validé, est réalisé par chaque
    filiale à hauteur de la part `CostAllocation` de SA course — Σ = 1001.00 au centime,
    rien ne reste à la filiale de la pièce. Une ligne du budget de B ne voit que la part de
    B, celle de A que la part de A ; une ligne groupe voit le tout, une fois."""
    trips, mission = _mission(sub_a, sub_b, requester_a, company_admin, vehicle)
    toll = _expense(sub_a, fleet_a, mission=mission, vehicle=vehicle, category="toll", amount="1001",
                    date=date(2025, 3, 12), status="to_validate")
    workflow.validate(toll, finance_a)

    lines = list(CostAllocation.objects.filter(expense=toll).select_related("trip"))
    allocated = {line.trip.subsidiary_id: line.amount for line in lines}
    assert len(lines) == 2 and set(allocated) == {sub_a.pk, sub_b.pk}
    assert sum(allocated.values()) == D("1001.00") and all(v > 0 for v in allocated.values())
    cells = _live(*MARCH)
    assert cells == {
        _key(sub_a, "toll"): _row(realised=allocated[sub_a.pk]),
        _key(sub_b, "toll"): _row(realised=allocated[sub_b.pk]),
    }, "la dépense de mission resterait entière sur la filiale de la pièce"
    assert sum(c["realised"] for c in cells.values()) == D("1001.00")

    finance_b = _user("fin-b-f3a@test.io", RoleChoices.FINANCE, sub_b)
    year = cells_for_year(2025)
    line_b = add_line(create_budget(actor=finance_b, year=2025, name="Dakar 2025", subsidiary=sub_b),
                      actor=finance_b, amount="1000", category="toll")
    line_a = add_line(create_budget(actor=finance_a, year=2025, name="Abidjan 2025", subsidiary=sub_a),
                      actor=finance_a, amount="1000", category="toll")
    line_group = add_line(create_budget(actor=company_admin, year=2025, name="Groupe 2025"),
                          actor=company_admin, amount="2000", category="toll")
    assert line_figures(line_b, year)["realised"] == allocated[sub_b.pk]
    assert line_figures(line_a, year)["realised"] == allocated[sub_a.pk]
    assert line_figures(line_group, year)["realised"] == D("1001.00")


def test_3b_engaged_mission_expense_is_split_provisionally_with_the_same_key(
        sub_a, sub_b, requester_a, fleet_a, finance_a, company_admin, vehicle):
    """Invariant 3 : tant qu'il n'est qu'ENGAGÉ (à valider), le péage de mission est réparti
    provisoirement entre A et B avec la clé passager-km de la mission — pas laissé entier
    sur la filiale de la pièce ; validé, la répartition définitive est la même."""
    trips, mission = _mission(sub_a, sub_b, requester_a, company_admin, vehicle)
    provisional = _provisional(mission, trips, "1001")
    toll = _expense(sub_a, fleet_a, mission=mission, vehicle=vehicle, category="toll", amount="1001",
                    date=date(2025, 3, 12), status="to_validate")
    assert _live(*MARCH) == {
        _key(sub_a, "toll"): _row(engaged=provisional[sub_a.pk]),
        _key(sub_b, "toll"): _row(engaged=provisional[sub_b.pk]),
    }, "l'engagé d'une mission reste entier sur la filiale de la pièce"
    workflow.validate(toll, finance_a)
    allocated = {line.trip.subsidiary_id: line.amount
                 for line in CostAllocation.objects.filter(expense=toll).select_related("trip")}
    assert allocated == provisional, "la répartition définitive suit la même clé"


def test_3c_approved_mission_adjustment_is_realised_by_its_allocation_lines(
        sub_a, sub_b, requester_a, fleet_a, finance_a, company_admin, vehicle):
    """Invariant 3 : un ajustement de MISSION approuvé est réalisé, au mois de
    comptabilisation, par chaque filiale à hauteur de ses lignes `CostAllocation`
    (Σ = 2002.00 au centime) ; le mois d'origine (mars) n'en voit rien."""
    trips, mission = _mission(sub_a, sub_b, requester_a, company_admin, vehicle)
    adjustment = create_adjustment(author=fleet_a, original=MARCH, amount="2002",
                                   reason="Péage de tournée oublié", subsidiary_id=sub_a.pk,
                                   source="mission", source_id=mission.pk, mission=mission,
                                   vehicle=vehicle, category="toll")
    approve_adjustment(adjustment, finance_a)
    allocated = {line.trip.subsidiary_id: line.amount
                 for line in CostAllocation.objects.filter(adjustment=adjustment).select_related("trip")}
    assert set(allocated) == {sub_a.pk, sub_b.pk} and sum(allocated.values()) == D("2002.00")
    assert _live(*_now()) == {
        _key(sub_a, "toll"): _row(realised=allocated[sub_a.pk]),
        _key(sub_b, "toll"): _row(realised=allocated[sub_b.pk]),
    }
    assert _live(*MARCH) == {}


def test_3d_pending_mission_adjustment_is_engaged_with_the_provisional_split(
        sub_a, sub_b, requester_a, fleet_a, company_admin, vehicle):
    """Invariant 3 : un ajustement de MISSION à approuver est ENGAGÉ sur le mois de
    comptabilisation, réparti provisoirement entre A et B avec la clé de la mission."""
    trips, mission = _mission(sub_a, sub_b, requester_a, company_admin, vehicle)
    provisional = _provisional(mission, trips, "2002")
    create_adjustment(author=fleet_a, original=MARCH, amount="2002", reason="Péage de tournée oublié",
                      subsidiary_id=sub_a.pk, source="mission", source_id=mission.pk, mission=mission,
                      vehicle=vehicle, category="toll")
    assert _live(*_now()) == {
        _key(sub_a, "toll"): _row(engaged=provisional[sub_a.pk]),
        _key(sub_b, "toll"): _row(engaged=provisional[sub_b.pk]),
    }, "l'engagé d'un ajustement de mission reste entier sur la filiale imputée"


# --- 4. Mois clos : figé ----------------------------------------------------------


def test_4_closed_month_is_frozen_in_budget_actuals(sub_a, fleet_a, finance_a, company_admin, vehicle):
    """Invariant 4 : la clôture de mars écrit dans `BudgetActual` exactement les cellules
    calculées à cet instant ; valider ensuite une dépense datée de mars ne change pas le
    RÉALISÉ ni le DÉCAISSÉ lus pour mars (elle arrive par ajustement sur le mois en cours) —
    seul son ENGAGÉ retombe (relu en direct : sinon elle compterait deux fois) ; les lignes
    figées ne se modifient ni ne se suppriment (ORM, instance comme queryset)."""
    center = CostCenter.objects.create(subsidiary=sub_a, code="LOG", name="Logistique")
    FuelLog.objects.create(vehicle=vehicle, subsidiary=sub_a, date=date(2025, 3, 10), liters="40",
                           amount="32000")
    _expense(sub_a, fleet_a, vehicle=vehicle, category="toll", amount="700", date=date(2025, 3, 10),
             cost_center=center, status="validated")
    _expense(sub_a, fleet_a, vehicle=vehicle, category="toll", amount="300", date=date(2025, 3, 11),
             cost_center=center, status="paid", paid_at=_paid_at(date(2025, 3, 20)))
    waiting = _expense(sub_a, fleet_a, vehicle=vehicle, category="parking", amount="450",
                       date=date(2025, 3, 18), status="to_validate")
    at_close = _live(*MARCH)
    assert at_close == {
        _key(sub_a, "energy"): _row(realised="32000"),
        _key(sub_a, "toll", center): _row(realised="1000", disbursed="300"),
        _key(sub_a, "parking"): _row(engaged="450"),
    }

    period, summary = close_period(*MARCH, company_admin)
    frozen = {(str(a.subsidiary_id), str(a.cost_center_id) if a.cost_center_id else None, a.category):
              {"engaged": a.engaged, "realised": a.realised, "disbursed": a.disbursed}
              for a in BudgetActual.objects.filter(period=period)}
    assert frozen == at_close and summary["budget_cells"] == 3
    assert _read(*MARCH) == at_close

    workflow.validate(waiting, finance_a)
    waiting.refresh_from_db()
    assert waiting.adjustment.status == FinancialAdjustment.APPROVED
    published = {key: values for key, values in at_close.items() if key != _key(sub_a, "parking")}
    assert _read(*MARCH) == published, "le réalisé du mois clos a bougé après une validation tardive"
    # La dépense n'est plus engagée en mars, et réalisée UNE fois, au mois de comptabilisation.
    assert _key(sub_a, "parking") not in _live(*MARCH)
    assert _live(*_now()) == {_key(sub_a, "parking"): _row(realised="450")}
    # Preuve que le réalisé vient du figé : une valeur figée l'emporte sur le recalcul.
    BudgetActual.objects.filter(period=period, category="energy").update(realised=D("31999"))
    assert _read(*MARCH)[_key(sub_a, "energy")]["realised"] == D("31999")
    BudgetActual.objects.filter(period=period, category="energy").update(realised=D("32000"))

    row = BudgetActual.objects.get(period=period, category="energy")
    with pytest.raises(FinancialHistoryLocked), transaction.atomic():
        row.realised = D("1")
        row.save()
    with pytest.raises(FinancialHistoryLocked), transaction.atomic():
        BudgetActual.objects.get(pk=row.pk).delete()
    with pytest.raises(FinancialHistoryLocked), transaction.atomic():
        BudgetActual.objects.filter(period=period).delete()
    assert BudgetActual.objects.filter(period=period).count() == 3
    assert BudgetActual.objects.get(pk=row.pk).realised == D("32000.00")
    assert _read(*MARCH) == published


# --- 5. Chiffres d'une ligne, axes, tableau de bord -------------------------------------


@pytest.fixture
def ledger(sub_a, sub_b, fleet_a):
    """Dépenses 2025 de la filiale A (et une de B) sur deux centres de coût."""
    cc1 = CostCenter.objects.create(subsidiary=sub_a, code="CC1", name="Logistique")
    cc2 = CostCenter.objects.create(subsidiary=sub_a, code="CC2", name="Direction")
    for category, amount, day, center, status, paid in (
        ("toll", "700", date(2025, 3, 10), None, "validated", None),
        ("toll", "300", date(2025, 3, 11), cc1, "to_validate", None),
        ("toll", "1000", date(2025, 5, 6), None, "validated", None),
        ("parking", "500", date(2025, 3, 12), cc1, "validated", None),
        ("parking", "100", date(2025, 3, 13), cc1, "paid", date(2025, 3, 25)),
        ("parking", "400", date(2025, 3, 14), cc1, "paid", date(2025, 4, 5)),  # payé en avril
        ("parking", "250", date(2025, 3, 15), cc2, "validated", None),
        ("parking", "50", date(2025, 3, 16), None, "validated", None),
    ):
        _expense(sub_a, fleet_a, category=category, amount=amount, date=day, cost_center=center,
                 status=status, paid_at=_paid_at(paid) if paid else None)
    _expense(sub_b, None, category="toll", amount="9999", date=date(2025, 3, 10), status="validated")
    return cc1, cc2


def test_5_line_figures_follow_the_axes_and_the_dashboard_sums_its_lines(
        sub_a, finance_a, company_admin, ledger):
    """Invariant 5 : par ligne, prévu / engagé / réalisé / décaissé / disponible = prévu −
    réalisé − engagé / taux. Ligne annuelle (tous mois), toutes catégories, tous centres :
    jokers ; centre de coût précis : correspondance EXACTE (ni l'autre centre, ni « sans
    centre ») ; une ligne de mars ne voit que le décaissé de mars. Les totaux du tableau de
    bord sont la somme exacte des lignes du budget."""
    cc1, cc2 = ledger
    budget = create_budget(actor=finance_a, year=2025, name="Abidjan 2025", subsidiary=sub_a)
    tolls = add_line(budget, actor=finance_a, amount="10000", category="toll", label="Péages")
    parking_cc1 = add_line(budget, actor=finance_a, amount="2000", month=3, cost_center_id=cc1.pk,
                           category="parking")
    parking_cc2 = add_line(budget, actor=finance_a, amount="500", month=3, cost_center_id=cc2.pk,
                           category="parking")
    year = cells_for_year(2025)

    assert line_figures(tolls, year) == {
        "planned": D("10000.00"), "engaged": D("300.00"), "realised": D("1700.00"),
        "disbursed": D("0.00"), "available": D("8000.00"), "rate": D("20.00"), "alert_level": None,
    }, "ligne annuelle tous centres : mars + mai, A seulement (le péage de B exclu)"
    assert line_figures(parking_cc1, year) == {
        "planned": D("2000.00"), "engaged": D("0.00"), "realised": D("1000.00"),
        "disbursed": D("100.00"), "available": D("1000.00"), "rate": D("50.00"), "alert_level": None,
    }, "centre CC1 exact ; décaissé de MARS seulement (le paiement d'avril n'y est pas)"
    assert line_figures(parking_cc2, year) == {
        "planned": D("500.00"), "engaged": D("0.00"), "realised": D("250.00"),
        "disbursed": D("0.00"), "available": D("250.00"), "rate": D("50.00"), "alert_level": None,
    }
    # Joker « toutes catégories, tous centres, toute l'année » (autre budget : pas de chevauchement).
    envelope = add_line(create_budget(actor=finance_a, year=2025, name="Enveloppe A", subsidiary=sub_a),
                        actor=finance_a, amount="5000", label="Toutes dépenses")
    assert line_figures(envelope, year) == {
        "planned": D("5000.00"), "engaged": D("300.00"), "realised": D("3000.00"),
        "disbursed": D("500.00"), "available": D("1700.00"), "rate": D("66.00"), "alert_level": None,
    }
    # Joker de filiale : une ligne de budget GROUPE sans filiale couvre A et B.
    group_line = add_line(create_budget(actor=company_admin, year=2025, name="Groupe 2025"),
                          actor=company_admin, amount="20000", category="toll")
    figures = line_figures(group_line, year)
    assert (figures["realised"], figures["engaged"]) == (D("11699.00"), D("300.00"))

    data = dashboard(finance_a, 2025, budget_id=budget.pk)
    assert data["totals"] == {"planned": "12500.00", "engaged": "300.00", "realised": "2950.00",
                              "disbursed": "100.00", "available": "9250.00", "rate": "26.00"}
    rows = {row["id"]: row for row in data["lines"]}
    assert set(rows) == {tolls.pk, parking_cc1.pk, parking_cc2.pk}
    for line in (tolls, parking_cc1, parking_cc2):
        expected = line_figures(line, year)
        assert {k: rows[line.pk][k] for k in ("planned", "engaged", "realised", "disbursed", "available")} \
            == {k: str(expected[k]) for k in ("planned", "engaged", "realised", "disbursed", "available")}
        assert rows[line.pk]["rate"] == str(expected["rate"])
    for measure in ("planned", "engaged", "realised", "disbursed", "available"):
        assert sum(D(r[measure]) for r in data["lines"]) == D(data["totals"][measure])


def test_5b_overlapping_lines_are_refused_so_a_total_never_counts_twice(sub_a, finance_a, ledger):
    """Invariant 5 : une ligne qui couvre une cellule déjà couverte (joker de mois, de
    catégorie, de centre de coût, filiale explicite = celle du budget) est refusée — rien
    n'est écrit, ni ligne ni historique ; une ligne voisine sans recouvrement est admise."""
    cc1, _cc2 = ledger
    budget = create_budget(actor=finance_a, year=2025, name="Abidjan 2025", subsidiary=sub_a)
    add_line(budget, actor=finance_a, amount="10000", category="toll")
    add_line(budget, actor=finance_a, amount="2000", month=3, cost_center_id=cc1.pk, category="parking")
    before = (BudgetLine.objects.count(), BudgetRevision.objects.count())
    for axes in (
        {"month": 5, "category": "toll"},                       # mai ⊂ ligne annuelle des péages
        {"category": ""},                                       # toutes catégories ⊃ tout
        {"month": 3, "category": "parking"},                    # tous centres ⊃ CC1
        {"month": 3, "category": "parking", "cost_center_id": cc1.pk, "subsidiary_id": sub_a.pk},
        {"month": None, "category": "parking", "cost_center_id": cc1.pk},  # année ⊃ mars
    ):
        with pytest.raises(BudgetError, match="chevauche"):
            add_line(budget, actor=finance_a, amount="1", **axes)
    assert (BudgetLine.objects.count(), BudgetRevision.objects.count()) == before
    add_line(budget, actor=finance_a, amount="300", month=4, category="parking", cost_center_id=cc1.pk)
    assert budget.lines.count() == 3


# --- 6. Révisions --------------------------------------------------------------------


def test_6_revisions_are_historised_reasoned_and_closed_months_stay_untouched(
        sub_a, finance_a, group_finance, company_admin):
    """Invariant 6 : création → révision « initial » ; brouillon → révisions « draft »
    libres ; approbation par un autre que l'auteur ; approuvé → motif obligatoire (rien
    d'écrit sans lui), révision « revision » ; mois clos : la ligne mensuelle ne se révise
    plus et aucune ligne de ce mois ne s'ajoute ; suppression de ligne en brouillon
    seulement ; l'historique ne se modifie ni ne se supprime."""
    budget = create_budget(actor=finance_a, year=2025, name="Abidjan 2025", subsidiary=sub_a)
    march = add_line(budget, actor=finance_a, amount="1000", month=3, category="toll")
    yearly = add_line(budget, actor=finance_a, amount="12000", category="parking")
    dropped = add_line(budget, actor=finance_a, amount="50", month=6, category="washing")

    def history(line):
        return list(line.revisions.order_by("at", "id").values_list(
            "kind", "previous_amount", "new_amount", "reason", "author_id"))

    assert history(march) == [("initial", None, D("1000.00"), "", finance_a.pk)]
    revise_line(march, actor=finance_a, amount="1500")
    revise_line(march, actor=finance_a, amount="1500")  # même montant : rien à historiser
    assert history(march) == [("initial", None, D("1000.00"), "", finance_a.pk),
                              ("draft", D("1000.00"), D("1500.00"), "", finance_a.pk)]
    initial = march.revisions.get(kind="initial")
    with pytest.raises(FinancialHistoryLocked), transaction.atomic():
        initial.reason = "réécrit"
        initial.save()

    remove_line(dropped, actor=finance_a)  # brouillon : la ligne part avec son historique
    assert not BudgetLine.objects.filter(pk=dropped.pk).exists()
    assert not BudgetRevision.objects.filter(line_id=dropped.pk).exists()

    with pytest.raises(BudgetError, match="autre personne"):
        approve(budget, actor=finance_a)
    budget.refresh_from_db()
    assert (budget.status, budget.approved_by_id, budget.approved_at) == (Budget.DRAFT, None, None)
    approve(budget, actor=group_finance)
    budget.refresh_from_db()
    assert (budget.status, budget.approved_by_id) == (Budget.APPROVED, group_finance.pk)

    for reason in ("", "   "):
        with pytest.raises(BudgetError, match="motif"):
            revise_line(yearly, actor=finance_a, amount="13000", reason=reason)
    yearly.refresh_from_db()
    assert yearly.amount == D("12000.00") and len(history(yearly)) == 1
    revise_line(yearly, actor=finance_a, amount="13000", reason="Hausse des tarifs de stationnement")
    assert history(yearly)[-1] == ("revision", D("12000.00"), D("13000.00"),
                                   "Hausse des tarifs de stationnement", finance_a.pk)
    with pytest.raises(BudgetError, match="motivée"):
        add_line(budget, actor=finance_a, amount="300", month=4, category="toll")
    april = add_line(budget, actor=finance_a, amount="300", month=4, category="toll", reason="Nouveau trajet")
    assert history(april) == [("revision", None, D("300.00"), "Nouveau trajet", finance_a.pk)]

    with pytest.raises(BudgetError, match="brouillon|révisez"):
        remove_line(yearly, actor=finance_a)
    assert BudgetLine.objects.filter(pk=yearly.pk).exists()

    close_period(*MARCH, company_admin)
    lines_before, revisions_before = BudgetLine.objects.count(), BudgetRevision.objects.count()
    with pytest.raises(BudgetError, match="Mois clos"):
        revise_line(march, actor=finance_a, amount="2000", reason="Rattrapage de mars")
    with pytest.raises(BudgetError, match="Mois clos"):
        add_line(budget, actor=finance_a, amount="100", month=3, category="washing", reason="Oubli")
    march.refresh_from_db()
    assert march.amount == D("1500.00")
    assert (BudgetLine.objects.count(), BudgetRevision.objects.count()) == (lines_before, revisions_before)
    add_line(budget, actor=finance_a, amount="100", month=5, category="washing", reason="Lavages de mai")

    # Budget approuvé : son historique ne se réécrit ni ne s'efface, sa ligne non plus.
    revision = march.revisions.get(kind="draft")
    with pytest.raises(FinancialHistoryLocked), transaction.atomic():
        revision.new_amount = D("1")
        revision.save()
    with pytest.raises(FinancialHistoryLocked), transaction.atomic():
        BudgetRevision.objects.get(pk=revision.pk).delete()
    with pytest.raises(ProtectedError), transaction.atomic():
        BudgetLine.objects.get(pk=march.pk).delete()
    assert history(march) == [("initial", None, D("1000.00"), "", finance_a.pk),
                              ("draft", D("1000.00"), D("1500.00"), "", finance_a.pk)]
