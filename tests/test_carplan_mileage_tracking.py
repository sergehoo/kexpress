"""Suivi kilométrique Car Plan : rythme réel entre relevés, fréquence 5 / 7 jours, rappels sans
doublon, corrections tracées, remplacement de compteur, fiabilité, confidentialité.

Le rythme est calculé sur la durée RÉELLE entre relevés (horodatage) ; avec deux relevés
seulement, l'estimation est « préliminaire » ; sans données suffisantes, aucune valeur.
"""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.utils import timezone
from rest_framework.test import APIClient

from apps.carplan import mileage, operations, services
from apps.carplan.models import CarPlanAssignment, CarPlanEvent, CarPlanInspection, MileageReading
from apps.carplan.services import CarPlanError
from apps.core.enums import NotificationType, RoleChoices
from apps.notifications.models import Notification
from apps.vehicles.models import Vehicle
from tests.test_carplan_c1 import _vehicle, category, eligible, employee, fleet_admin  # noqa: F401
from tests.test_finance_f1 import _user

pytestmark = pytest.mark.django_db

A = CarPlanAssignment
TODAY = timezone.localdate()


# --- Aides ----------------------------------------------------------------------------------


def _point(days_ago: float, km: int, now=None):
    now = now or timezone.now()
    return SimpleNamespace(recorded_at=now - timedelta(days=days_ago), index=km, odometer=km)


def _series(*steps, start_km=10_000):
    """steps : (jours écoulés depuis le relevé précédent, km parcourus) → relevés triés."""
    now = timezone.now()
    total_days = sum(d for d, _ in steps)
    points, km, elapsed = [_point(total_days, start_km, now)], start_km, 0
    for days, driven in steps:
        elapsed += days
        km += driven
        points.append(_point(total_days - elapsed, km, now))
    return points


def running(policy, beneficiary, vehicle, fleet_a, fleet_admin, *, days_ago=40, start_km=10_000):
    """Attribution ACTIVE dont la remise (et son relevé) date de `days_ago` jours."""
    a = services.request_assignment(actor=fleet_a, beneficiary=beneficiary, policy=policy,
                                    assignment_type="company_car", start_date=TODAY - timedelta(days=days_ago),
                                    planned_end_date=TODAY + timedelta(days=200))
    services.validate_assignment(a, actor=fleet_admin)
    services.allocate_vehicle(a, vehicle, actor=fleet_a)
    at = timezone.now() - timedelta(days=days_ago)
    handover = CarPlanInspection.objects.create(
        assignment=a, vehicle=vehicle, kind="handover", performed_at=at, mileage=start_km, energy_level_pct=80,
        exterior_condition="good", interior_condition="good", performed_by=fleet_a, employee_signed_at=at,
        manager_signed_at=at, manager_signed_by=fleet_a)
    MileageReading.objects.create(assignment=a, vehicle=vehicle, reading_date=timezone.localdate(at), recorded_at=at,
                                  odometer=start_km, source="handover", declared_by=fleet_a)
    services.activate(a, actor=fleet_a, handover=handover)
    Vehicle.objects.filter(pk=vehicle.pk).update(mileage=start_km)
    a.refresh_from_db()
    return a


def reading(a, days_ago, km, *, by=None):
    at = timezone.now() - timedelta(days=days_ago)
    r = MileageReading.objects.create(assignment=a, vehicle=a.vehicle, reading_date=timezone.localdate(at),
                                      recorded_at=at, odometer=km, declared_by=by or a.beneficiary)
    operations.raise_vehicle_mileage(a.vehicle, km)
    return r


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def car(sub_a):
    return _vehicle(sub_a, "CP-KM-01")


@pytest.fixture
def km_policy(sub_a, fleet_a, fleet_admin, category):  # noqa: F811
    """Politique en vigueur depuis 200 jours (les attributions de ces tests commencent dans le passé)."""
    p = services.create_policy(actor=fleet_a, code="KM", name="Suivi km", subsidiary=sub_a,
                               effective_from=TODAY - timedelta(days=200))
    version = p.versions.get()
    services.update_draft(version, actor=fleet_a, categories=[category], assignment_types=["company_car", "service"],
                          max_duration_months=24)
    services.publish_version(version, actor=fleet_admin)
    return p


@pytest.fixture
def active(km_policy, eligible, car, fleet_a, fleet_admin):  # noqa: F811
    return running(km_policy, eligible, car, fleet_a, fleet_admin)


def _no_money(payload) -> bool:
    text = str(payload).lower()
    return not any(word in text for word in ("cost", "amount", "montant", "coût", "xof", "price"))


# =====================================================================================
# Rythme : durée réelle entre relevés
# =====================================================================================


def test_pace_uses_the_real_duration_between_two_readings():
    """3 000 → 3 500 km à 24 h d'écart = 500 km / jour, estimation préliminaire."""
    pace = mileage.estimate_pace([_point(1, 3000), _point(0, 3500)])
    assert pace["km_per_day"] == 500 and pace["method"] == "preliminary"
    assert pace["label"].startswith("Estimation préliminaire")
    # Même distance sur 36 h : la DURÉE fait le rythme, pas le changement de date.
    assert mileage.estimate_pace([_point(1.5, 3000), _point(0, 3500)])["km_per_day"] == pytest.approx(333.3, abs=0.1)


@pytest.mark.parametrize("gap", [1, 5, 7])
def test_regular_readings_every_1_5_or_7_days_give_the_same_pace(gap):
    pace = mileage.estimate_pace(_series(*[(gap, 60 * gap)] * 4))
    assert pace["km_per_day"] == pytest.approx(60, abs=0.1)
    assert pace["method"] == "weighted" and pace["intervals"] == 4


def test_mixed_spacing_1_5_7_days_is_weighted_by_duration():
    pace = mileage.estimate_pace(_series((1, 80), (5, 400), (7, 560)))
    assert pace["km_per_day"] == pytest.approx(80, abs=0.1)


def test_insufficient_data_gives_no_estimate():
    assert mileage.estimate_pace([])["km_per_day"] is None
    assert mileage.estimate_pace([_point(3, 1000)])["km_per_day"] is None
    # Deux saisies à deux heures d'écart ne mesurent pas un rythme.
    close = mileage.estimate_pace([_point(2 / 24, 1000), _point(0, 1100)])
    assert close["km_per_day"] is None and close["method"] is None and close["label"] == "Données insuffisantes"


def test_two_readings_are_preliminary_several_are_weighted():
    assert mileage.estimate_pace(_series((7, 350)))["method"] == "preliminary"
    assert mileage.estimate_pace(_series((7, 350), (7, 350)))["method"] == "weighted"


def test_a_change_of_pace_moves_the_estimate_towards_recent_readings():
    points = _series(*[(7, 350)] * 4, (7, 1050), (7, 1050))  # 50 km/j puis 150 km/j
    pace = mileage.estimate_pace(points)
    overall = (points[-1].index - points[0].index) / 42
    assert pace["km_per_day"] > overall and pace["km_per_day"] > 100
    assert pace["recent_km_per_day"] == pytest.approx(150, abs=0.1) and pace["pace_increase"] is True


def test_an_exceptional_value_is_clipped_around_the_median():
    pace = mileage.estimate_pace(_series((7, 350), (7, 350), (1, 2000), (7, 350), (7, 350)))
    assert pace["km_per_day"] < 120  # sans écrêtage, l'intervalle d'un jour à 2 000 km pèserait bien plus


# =====================================================================================
# Fréquence, échéance du relevé, rappels sans doublon
# =====================================================================================


def test_frequency_comes_from_the_policy_unless_the_assignment_overrides_it(api, active, fleet_a, eligible):
    assert mileage.frequency_for(active) == (7, "politique")
    api.force_authenticate(fleet_a)
    url = f"/api/carplan/assignments/{active.pk}/reading-frequency/"
    assert api.post(url, {"days": 6}, format="json").status_code == 400
    r = api.post(url, {"days": 5}, format="json")
    assert r.status_code == 200 and r.json()["reading_frequency_days"] == 5
    active.refresh_from_db()
    assert mileage.frequency_for(active) == (5, "attribution")
    assert CarPlanEvent.objects.filter(assignment=active, kind="reading_frequency").exists()
    api.force_authenticate(eligible)  # le bénéficiaire ne règle pas sa propre fréquence
    assert api.post(url, {"days": 7}, format="json").status_code == 403


def test_reading_status_due_late_and_stale(active):
    reading(active, 8, 10_400)  # dernier relevé il y a 8 jours, fréquence 7
    status = mileage.reading_status(active)
    assert status["state"] == "due" and status["late_days"] == 1
    assert status["next_due"] == (TODAY - timedelta(days=1)).isoformat()
    later = timezone.now() + timedelta(days=3)
    assert mileage.reading_status(active, now=later)["state"] == "late"
    much_later = timezone.now() + timedelta(days=7)
    stale = mileage.reading_status(active, now=much_later)
    assert stale["state"] == "stale" and stale["stale"] is True


def test_one_reminder_per_due_date_then_one_relaunch_then_managers(active, eligible, fleet_a):
    reading(active, 7, 10_350)  # relevé dû aujourd'hui
    kinds = Notification.objects.filter(notification_type=NotificationType.MILEAGE_READING_DUE)
    assert mileage.check_reading_reminders(TODAY) == {"reminders": 1, "relaunches": 0, "escalations": 0}
    assert mileage.check_reading_reminders(TODAY)["reminders"] == 0  # jamais deux fois
    mine = kinds.filter(recipient=eligible)
    assert mine.count() == 1 and mine.get().link.startswith("/my-vehicle")
    assert mileage.check_reading_reminders(TODAY + timedelta(days=1)) == {"reminders": 0, "relaunches": 0,
                                                                          "escalations": 0}
    assert mileage.check_reading_reminders(TODAY + timedelta(days=3))["relaunches"] == 1
    assert mileage.check_reading_reminders(TODAY + timedelta(days=4))["relaunches"] == 0
    assert mileage.check_reading_reminders(TODAY + timedelta(days=7))["escalations"] == 1
    assert mileage.check_reading_reminders(TODAY + timedelta(days=9)) == {"reminders": 0, "relaunches": 0,
                                                                          "escalations": 0}
    assert kinds.filter(recipient=eligible).count() == 2
    assert Notification.objects.filter(recipient=fleet_a, title__contains="relevé kilométrique en retard").count() == 1
    # Un nouveau relevé ouvre une nouvelle échéance : un nouveau rappel, le moment venu.
    operations.declare_mileage(active, actor=eligible, odometer=10_700)
    assert mileage.check_reading_reminders(TODAY)["reminders"] == 0
    assert mileage.check_reading_reminders(TODAY + timedelta(days=7))["reminders"] == 1


def test_no_reminder_when_the_policy_requires_no_declaration(active):
    version = active.policy_version
    # Version publiée immuable : on simule une politique « aucune déclaration » sur une copie en mémoire.
    version.mileage_declaration = "none"
    status = mileage.reading_status(active)
    assert status["state"] == "not_required" and status["required"] is False


def test_the_monthly_check_runs_the_periodic_reminders(active):
    reading(active, 7, 10_350)
    sent = operations.check_assignments()
    assert sent["declaration"] == 1


# =====================================================================================
# Corrections tracées
# =====================================================================================


def test_the_beneficiary_corrects_his_last_declaration_within_48_hours(api, active, eligible, car, monkeypatch):
    reading(active, 3, 10_200)
    api.force_authenticate(eligible)
    r = api.post("/api/carplan/me/mileage/", {"odometer": 12_500}, format="json")  # faute de frappe : 10 500
    assert r.status_code == 201
    wrong = r.json()["id"]
    car.refresh_from_db()
    assert car.mileage == 12_500
    tracking = api.get("/api/carplan/me/tracking/").json()
    assert tracking["correctable_reading"] == wrong
    url = f"/api/carplan/me/mileage/{wrong}/correct/"
    assert api.post(url, {"odometer": 10_100}, format="json").status_code == 400  # < relevé précédent
    r = api.post(url, {"odometer": 10_500}, format="json")
    assert r.status_code == 201, r.content
    car.refresh_from_db()
    assert car.mileage == 10_500  # le compteur suit la correction du dernier relevé
    history = api.get("/api/carplan/me/mileage/").json()
    original = next(h for h in history if h["id"] == wrong)
    corrected = next(h for h in history if h["corrects"] == wrong)
    assert original["superseded"] and original["corrected_by_id"] == corrected["id"]
    assert corrected["odometer"] == 10_500 and corrected["reason"]
    # Le relevé corrigé sort des calculs, il reste en base.
    assert not MileageReading.objects.filter(pk=wrong).exists()
    assert MileageReading.all_objects.filter(pk=wrong, odometer=12_500).exists()
    assert operations._last_reading(car).odometer == 10_500
    assert api.post(url, {"odometer": 10_400}, format="json").status_code == 400  # déjà corrigé
    event = CarPlanEvent.objects.get(assignment=active, kind="mileage_corrected")
    assert event.details["previous"] == 12_500 and event.details["odometer"] == 10_500
    # Hors délai : la correction passe par le gestionnaire.
    monkeypatch.setattr(mileage, "CORRECTION_WINDOW", timedelta(seconds=0))
    assert api.post(f"/api/carplan/me/mileage/{corrected['id']}/correct/", {"odometer": 10_450},
                    format="json").status_code == 400


def test_a_reading_is_never_rewritten_in_place(active):
    r = reading(active, 2, 10_100)
    with pytest.raises(ProtectedError), transaction.atomic():
        r.odometer = 10_050
        r.save()
    with pytest.raises(IntegrityError), transaction.atomic():
        MileageReading.objects.filter(pk=r.pk).update(odometer=10_050)  # trigger PostgreSQL
    with pytest.raises(ProtectedError), transaction.atomic():
        r.delete()


def test_the_beneficiary_only_corrects_his_own_latest_declaration(api, active, eligible, fleet_a, sub_a):
    older = reading(active, 3, 10_200)
    api.force_authenticate(eligible)
    assert api.post("/api/carplan/me/mileage/", {"odometer": 10_300}, format="json").status_code == 201
    assert api.post(f"/api/carplan/me/mileage/{older.pk}/correct/", {"odometer": 10_150},
                    format="json").status_code == 400  # pas la dernière
    by_manager = operations.declare_mileage(active, actor=fleet_a, odometer=10_400, by_manager=True)
    assert api.post(f"/api/carplan/me/mileage/{by_manager.pk}/correct/", {"odometer": 10_350},
                    format="json").status_code == 400  # relevé du gestionnaire
    colleague = _user("km-coll@test.io", RoleChoices.REQUESTER, sub_a)
    api.force_authenticate(colleague)
    assert api.post(f"/api/carplan/me/mileage/{by_manager.pk}/correct/", {"odometer": 10_350},
                    format="json").status_code == 404


def test_a_manager_correction_needs_a_reason_stays_between_neighbours_and_informs_the_beneficiary(
        api, active, fleet_a, eligible):
    a = reading(active, 10, 10_300)
    b = reading(active, 5, 13_000)  # erreur ancienne (au-delà du délai du bénéficiaire)
    reading(active, 1, 10_900)  # incohérent avec b : la correction de b le résout
    api.force_authenticate(fleet_a)
    url = f"/api/carplan/assignments/{active.pk}/mileage/{b.pk}/correct/"
    assert api.post(url, {"odometer": 10_600}, format="json").status_code == 400  # motif
    assert api.post(url, {"odometer": 10_200, "reason": "x"}, format="json").status_code == 400  # < précédent
    assert api.post(url, {"odometer": 11_000, "reason": "x"}, format="json").status_code == 400  # > suivant
    r = api.post(url, {"odometer": 10_600, "reason": "Chiffre inversé"}, format="json")
    assert r.status_code == 201, r.content
    note = Notification.objects.get(recipient=eligible, title="Relevé kilométrique corrigé")
    assert "13000" in note.message and "10600" in note.message and "Chiffre inversé" in note.message
    effective = [x.odometer for x in MileageReading.objects.filter(assignment=active).order_by("recorded_at", "id")]
    assert effective == [10_000, 10_300, 10_600, 10_900]
    assert a.pk in [x.pk for x in MileageReading.objects.all()]
    rel = mileage.reliability(active)
    assert rel["corrections"] == 1 and any("correction" in f for f in rel["factors"])
    # Le bénéficiaire ne passe pas par l'API de gestion ; un auditeur n'écrit pas.
    api.force_authenticate(eligible)
    assert api.post(url, {"odometer": 10_650, "reason": "x"}, format="json").status_code == 403


def test_an_atypical_reading_is_accepted_but_flagged(active, eligible, fleet_a):
    for days, km in ((28, 10_350), (21, 10_700), (14, 11_050), (7, 11_400)):
        reading(active, days, km)
    r = operations.declare_mileage(active, actor=eligible, odometer=14_000)  # 371 km/j contre 50
    assert r.anomaly and "habituel" in r.anomaly
    assert Notification.objects.filter(recipient=fleet_a, title__contains="atypique").exists()
    assert mileage.reliability(active)["anomalies"] == 1


# =====================================================================================
# Remplacement de compteur
# =====================================================================================


def test_meter_replacement_keeps_the_history_usable_and_shifts_maintenance_thresholds(
        api, active, fleet_a, eligible, car):
    from apps.maintenance.models import MaintenanceSchedule
    from apps.maintenance.predictive import ensure_plans, recompute_thresholds
    from apps.vehicles.compliance import next_revision_km
    from apps.vehicles.models import VehicleRevision

    VehicleRevision.objects.create(vehicle=car, date=TODAY - timedelta(days=60), mileage_at_revision=9_000)
    for days, km in ((28, 10_600), (21, 10_950), (14, 11_300), (7, 11_650)):  # 50 km / jour
        reading(active, days, km)
    ensure_plans(car)
    oil = MaintenanceSchedule.objects.get(vehicle=car, maintenance_type__kind="oil_change")
    oil.last_done_mileage, oil.last_done_date = 9_500, TODAY - timedelta(days=50)
    recompute_thresholds(oil)
    oil.save()
    assert oil.due_mileage == 19_500
    api.force_authenticate(fleet_a)
    url = f"/api/carplan/assignments/{active.pk}/meter-replacement/"
    assert api.post(url, {"old_odometer": 11_850, "new_odometer": 0}, format="json").status_code == 400  # motif
    assert api.post(url, {"old_odometer": 11_000, "new_odometer": 0, "reason": "x"},
                    format="json").status_code == 400  # ancien compteur < dernier relevé
    at = (timezone.now() - timedelta(days=3)).isoformat()
    r = api.post(url, {"old_odometer": 11_850, "new_odometer": 0, "reason": "Combiné remplacé", "recorded_at": at},
                 format="json")
    assert r.status_code == 201, r.content
    car.refresh_from_db()
    assert car.mileage == 0
    api.force_authenticate(eligible)
    assert api.post("/api/carplan/me/mileage/", {"odometer": 150}, format="json").status_code == 201
    car.refresh_from_db()
    assert car.mileage == 150
    # Le rythme enjambe le changement : 50 km / jour avant comme après.
    assert mileage.pace_for(car, active)["km_per_day"] == pytest.approx(50, abs=1)
    # Kilomètres de l'attribution : 1 850 sur l'ancien compteur + 150 sur le nouveau.
    assert operations._km_between(active, active.start_date, TODAY) == 2_000
    # Conformité : prochaine révision sur le nouveau compteur (9 000 + 10 000 − 11 850).
    assert next_revision_km(car) == 7_150
    oil.refresh_from_db()
    assert oil.due_mileage == 19_500 - 11_850 and oil.last_done_mileage == 9_500 - 11_850
    assert api.post("/api/carplan/me/mileage/", {"odometer": 100}, format="json").status_code == 400  # recule
    assert CarPlanEvent.objects.filter(assignment=active, kind="meter_replaced").exists()
    # Comparaison remise / restitution : les km de l'ancien compteur sont comptés.
    from apps.carplan.inspections import compare

    back = CarPlanInspection(assignment=active, vehicle=car, kind="return", performed_at=timezone.now(), mileage=150,
                             energy_level_pct=50, exterior_condition="good", interior_condition="good")
    assert compare(active.inspections.get(kind="handover"), back)["km_driven"] == 2_000


# =====================================================================================
# Suivi, fiabilité, confidentialité
# =====================================================================================


def test_tracking_shows_pace_reliability_and_next_operation_without_any_amount(api, active, eligible, car):
    from apps.maintenance.models import MaintenanceSchedule
    from apps.maintenance.predictive import ensure_plans, recompute_thresholds

    for days, km in ((28, 10_600), (21, 10_950), (14, 11_300), (7, 11_650), (0.5, 11_975)):
        reading(active, days, km)
    ensure_plans(car)
    oil = MaintenanceSchedule.objects.get(vehicle=car, maintenance_type__kind="oil_change")
    oil.last_done_mileage, oil.last_done_date = 2_475, TODAY - timedelta(days=200)
    recompute_thresholds(oil)
    oil.save()  # seuil 12 475 km : 500 km restants à 50 km / jour → environ 10 jours
    api.force_authenticate(eligible)
    data = api.get("/api/carplan/me/tracking/").json()
    assert data["pace"]["method"] == "weighted" and data["pace"]["km_per_day"] == pytest.approx(50, abs=1)
    assert data["reliability"]["level"] == "good" and data["reliability"]["score"] >= 75
    assert data["reading"]["state"] == "ok" and data["current_odometer"] == 11_975
    nxt = data["next_operation"]
    assert nxt["kind"] == "oil_change" and nxt["remaining_km"] == 500 and nxt["trigger"] == "km"
    assert 9 <= nxt["days_left"] <= 10 and nxt["level"] == "notice"
    assert _no_money(data) and _no_money(api.get("/api/carplan/me/").json())


def test_two_readings_only_show_a_preliminary_estimate(api, active, eligible):
    reading(active, 0.5, 10_400)
    api.force_authenticate(eligible)
    pace = api.get("/api/carplan/me/tracking/").json()["pace"]
    assert pace["method"] == "preliminary" and "préliminaire" in pace["label"]


def test_tracking_is_private_to_the_beneficiary_and_the_managers_perimeter(
        api, active, sub_a, sub_b, fleet_a):
    colleague = _user("km-coll2@test.io", RoleChoices.REQUESTER, sub_a)
    api.force_authenticate(colleague)
    assert api.get("/api/carplan/me/tracking/").status_code == 404
    assert api.get(f"/api/carplan/assignments/{active.pk}/tracking/").status_code == 403
    assert api.get("/api/carplan/mileage-followup/").status_code == 403
    other = _user("km-fleet-b@test.io", RoleChoices.FLEET_MANAGER, sub_b)
    api.force_authenticate(other)
    assert api.get(f"/api/carplan/assignments/{active.pk}/tracking/").status_code == 404
    assert api.get("/api/carplan/mileage-followup/").json()["count"] == 0
    assert api.post(f"/api/carplan/assignments/{active.pk}/reading-frequency/", {"days": 5},
                    format="json").status_code == 404
    api.force_authenticate(fleet_a)
    rows = api.get("/api/carplan/mileage-followup/").json()["results"]
    assert [r["reference"] for r in rows] == [active.reference]


def test_followup_lists_late_and_stale_readings_first(api, km_policy, eligible, car, fleet_a, fleet_admin, sub_a,  # noqa: F811
                                                      category):
    from apps.carplan.models import CarPlanProfile

    stale = running(km_policy, eligible, car, fleet_a, fleet_admin, days_ago=20)  # aucun relevé depuis 20 jours
    other = _user("km-emp2@test.io", RoleChoices.REQUESTER, sub_a)
    CarPlanProfile.objects.create(user=other, category=category)
    fresh = running(km_policy, other, _vehicle(sub_a, "CP-KM-02"), fleet_a, fleet_admin, days_ago=10)
    reading(fresh, 1, 10_500)
    api.force_authenticate(fleet_a)
    rows = api.get("/api/carplan/mileage-followup/").json()["results"]
    assert [r["reference"] for r in rows] == [stale.reference, fresh.reference]
    assert rows[0]["reading"]["state"] == "stale" and rows[1]["reading"]["state"] == "ok"


def test_a_beneficiary_cannot_correct_a_reading_made_by_someone_else(active, eligible, fleet_a):
    with pytest.raises(CarPlanError, match="propres déclarations"):
        r = reading(active, 1, 10_100, by=fleet_a)
        mileage.correct_reading(r, actor=eligible, odometer=10_050)
