"""Plans d'entretien prédictifs : seuils km / calendaire concurrents, thermique / hybride /
électrique, prévision depuis les relevés, alertes mémorisées (sans doublon, relances, rythme en
hausse), clôture par une intervention terminée, registres existants (visite, assurance,
révision), périmètre de filiale."""
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.carplan.models import CarPlanProfile
from apps.core.enums import MaintenanceStatus, NotificationType, RoleChoices
from apps.maintenance import predictive
from apps.maintenance.models import MaintenancePlanEvent, MaintenanceRecord, MaintenanceSchedule, MaintenanceType
from apps.notifications.models import Notification
from apps.vehicles.models import InsurancePolicy, TechnicalInspection, Vehicle, VehicleRevision
from tests.test_carplan_c1 import _vehicle, category, eligible, employee, fleet_admin  # noqa: F401
from tests.test_carplan_mileage_tracking import _no_money, active, api, car, km_policy, reading, running  # noqa: F401
from tests.test_finance_f1 import _user

pytestmark = pytest.mark.django_db

TODAY = timezone.localdate()
FORECAST = NotificationType.MAINTENANCE_FORECAST


def _steady(a, *, per_day=50, start_km=10_000, days=(28, 21, 14, 7, 0.5)):
    """Relevés réguliers depuis la remise (40 jours) : `per_day` km / jour."""
    for d in days:
        reading(a, d, start_km + round(per_day * (40 - d)))


def _plan(vehicle, kind, *, done_km=None, done_days_ago=None, **extra):
    predictive.ensure_plans(vehicle)
    plan = MaintenanceSchedule.objects.get(vehicle=vehicle, maintenance_type__kind=kind)
    plan.last_done_mileage = done_km
    plan.last_done_date = TODAY - timedelta(days=done_days_ago) if done_days_ago is not None else None
    for field, value in extra.items():
        setattr(plan, field, value)
    predictive.recompute_thresholds(plan, vehicle)
    plan.save()
    return plan


def _row(vehicle, kind):
    return next(r for r in predictive.vehicle_outlook(vehicle)["plans"] if r["kind"] == kind)


def _forecasts(user):
    return Notification.objects.filter(recipient=user, notification_type=FORECAST)


# =====================================================================================
# Plans par motorisation
# =====================================================================================


@pytest.mark.parametrize("fuel, thermal", [("diesel", True), ("hybrid", True), ("electric", False)])
def test_reference_plans_follow_the_energy_of_the_vehicle(sub_a, fuel, thermal):
    vehicle = _vehicle(sub_a, f"CP-PL-{fuel[:3].upper()}")
    Vehicle.objects.filter(pk=vehicle.pk).update(fuel_type=fuel)
    vehicle.refresh_from_db()
    predictive.ensure_plans(vehicle)
    predictive.ensure_plans(vehicle)  # idempotent
    kinds = sorted(MaintenanceSchedule.objects.filter(vehicle=vehicle).values_list("maintenance_type__kind", flat=True))
    expected = {"tyres", "brakes", "general_service"} | ({"oil_change", "filters"} if thermal else set())
    assert kinds == sorted(expected)
    assert MaintenanceType.objects.filter(kind="oil_change", combustion_only=True).count() == 1


def test_an_electric_vehicle_gets_no_engine_oil_change(api, sub_a, fleet_a):
    vehicle = _vehicle(sub_a, "CP-PL-EV2")
    Vehicle.objects.filter(pk=vehicle.pk).update(fuel_type="electric")
    predictive.ensure_default_types()
    oil = MaintenanceType.objects.get(kind="oil_change")
    api.force_authenticate(fleet_a)
    r = api.post("/api/maintenance-plans/", {"vehicle": str(vehicle.pk), "maintenance_type": str(oil.pk)},
                 format="json")
    assert r.status_code == 400
    # Un plan thermique laissé sur un véhicule devenu électrique est ignoré.
    MaintenanceSchedule.objects.create(vehicle=vehicle, maintenance_type=oil, due_mileage=5)
    vehicle.refresh_from_db()
    assert all(r["kind"] != "oil_change" for r in predictive.vehicle_outlook(vehicle)["plans"])


# =====================================================================================
# Seuils, prévision, niveaux
# =====================================================================================


def test_km_and_calendar_limits_compete(active, car):
    _steady(active)  # 50 km / jour, compteur 11 975
    _plan(car, "oil_change", done_km=9_000, done_days_ago=300)  # 19 000 km ou dans 65 jours
    row = _row(car, "oil_change")
    assert row["due_mileage"] == 19_000 and row["remaining_km"] == 7_025
    assert row["next_date"] == (TODAY + timedelta(days=65)).isoformat()
    assert row["trigger"] == "date" and row["expected_date"] == row["next_date"] and row["level"] == "ok"
    _plan(car, "oil_change", done_km=2_400, done_days_ago=100)  # 12 400 km : 425 km à 50 km / jour
    row = _row(car, "oil_change")
    assert row["trigger"] == "km" and row["remaining_km"] == 425
    assert row["forecast_km_date"] == row["expected_date"] and 8 <= row["days_left"] <= 9
    assert row["level"] == "notice" and row["forecast_method"] == "weighted"


def test_reaching_the_km_or_the_date_is_overdue(active, car):
    _steady(active)
    _plan(car, "oil_change", done_km=1_000, done_days_ago=20)  # 11 000 km atteints
    row = _row(car, "oil_change")
    assert row["level"] == "overdue" and row["km_reached"] and row["trigger"] == "km"
    # Seuil franchi avant la limite calendaire : l'échéance est aujourd'hui, jamais une date future.
    assert row["date_limit"] > TODAY.isoformat()
    assert row["expected_date"] == TODAY.isoformat() and row["days_left"] == 0
    _title, beneficiary_msg, _manager = predictive._messages(row, "alert")
    assert "dans " not in beneficiary_msg and "seuil de 11000 km atteint" in beneficiary_msg
    _plan(car, "brakes", done_km=11_000, done_days_ago=10, due_date=TODAY)  # date limite imposée : aujourd'hui
    row = _row(car, "brakes")
    assert row["level"] == "overdue" and row["date_reached"] and not row["km_reached"]
    assert row["deadline"] == TODAY.isoformat() and row["trigger"] == "date"


def test_no_forecast_date_is_invented_without_enough_readings(active, car):
    _plan(car, "oil_change", done_km=5_000, done_days_ago=100)  # seule la remise est connue
    row = _row(car, "oil_change")
    assert row["forecast_km_date"] is None and row["forecast_label"] == "Données insuffisantes"
    assert row["expected_date"] == (TODAY + timedelta(days=265)).isoformat() and row["trigger"] == "date"
    reading(active, 0.5, 10_400)  # deux relevés : estimation préliminaire
    row = _row(car, "oil_change")
    assert row["preliminary"] is True and row["forecast_km_date"] is not None


def test_the_thresholds_are_configurable_per_operation(active, car):
    _steady(active)
    _plan(car, "oil_change", done_km=2_400, done_days_ago=100)  # ≈ 8,5 jours
    assert _row(car, "oil_change")["level"] == "notice"
    MaintenanceType.objects.filter(kind="oil_change").update(notice_days=30, alert_days=10, urgent_days=5)
    assert _row(car, "oil_change")["level"] == "alert"


# =====================================================================================
# Alertes mémorisées
# =====================================================================================


def test_alerts_are_memorized_escalate_once_and_relaunch_when_due(active, car, eligible, fleet_a, sub_b):
    other_manager = _user("pl-fleet-b@test.io", RoleChoices.FLEET_MANAGER, sub_b)
    _steady(active)
    plan = _plan(car, "oil_change", done_km=2_400, done_days_ago=100)  # préavis
    predictive.check_plans()
    predictive.check_plans()
    assert _forecasts(eligible).count() == 1 and _forecasts(fleet_a).count() == 0  # préavis : bénéficiaire seul
    plan.refresh_from_db()
    assert plan.alert_level == "notice"
    # Alerte (≤ 7 jours) : bénéficiaire ET gestionnaires de la filiale, une fois.
    _plan(car, "oil_change", done_km=2_200, done_days_ago=100)
    predictive.check_plans()
    predictive.check_plans()
    assert _forecasts(eligible).count() == 2 and _forecasts(fleet_a).count() == 1
    assert _forecasts(other_manager).count() == 0
    # Urgence (≤ 3 jours), puis relance trois jours plus tard seulement.
    _plan(car, "oil_change", done_km=2_050, done_days_ago=100)
    predictive.check_plans()
    assert _forecasts(eligible).count() == 3
    predictive.check_plans()
    assert _forecasts(eligible).count() == 3
    MaintenanceSchedule.objects.filter(pk=plan.pk).update(alert_notified_at=timezone.now() - timedelta(days=4))
    predictive.check_plans()
    assert _forecasts(eligible).count() == 4 and _forecasts(eligible).latest("created_at").title.startswith("Relance")
    # Niveau qui redescend (seuil repoussé) : aucune notification, l'historique le note.
    _plan(car, "oil_change", done_km=2_400, done_days_ago=100)
    predictive.check_plans()
    assert _forecasts(eligible).count() == 4
    kinds = list(MaintenancePlanEvent.objects.filter(schedule=plan).values_list("kind", flat=True))
    assert kinds.count("alert") == 3 and kinds.count("relaunch") == 1 and "cleared" in kinds
    assert _no_money(list(_forecasts(eligible).values("title", "message")))


def test_overdue_is_critical_and_relaunched_weekly(active, car, eligible, fleet_a):
    _steady(active)
    plan = _plan(car, "oil_change", done_km=1_000, done_days_ago=20)
    predictive.check_plans()
    note = _forecasts(eligible).get()
    assert note.severity == "critical" and "dépassée" in note.title and note.link.startswith("/my-vehicle")
    assert _forecasts(fleet_a).get().link == f"/maintenance?plan={plan.pk}"
    MaintenanceSchedule.objects.filter(pk=plan.pk).update(alert_notified_at=timezone.now() - timedelta(days=5))
    predictive.check_plans()
    assert _forecasts(eligible).count() == 1
    MaintenanceSchedule.objects.filter(pk=plan.pk).update(alert_notified_at=timezone.now() - timedelta(days=8))
    predictive.check_plans()
    assert _forecasts(eligible).count() == 2


def test_a_marked_increase_of_pace_sends_one_early_warning(active, car, eligible, fleet_a):
    for d, km in ((35, 10_250), (28, 10_600), (21, 10_950), (14, 11_300)):  # 50 km / jour
        reading(active, d, km)
    plan = _plan(car, "oil_change", done_km=5_325, done_days_ago=100)  # seuil 15 325 km
    predictive.refresh_vehicle(car, notify=False)  # prévision mémorisée au rythme habituel
    plan.refresh_from_db()
    before = plan.forecast_date
    assert before is not None and (before - TODAY).days > 40
    reading(active, 7, 12_350)  # 150 km / jour
    reading(active, 0.5, 13_325)
    rows = predictive.refresh_vehicle(car, notify=True)
    row = next(r for r in rows if r["kind"] == "oil_change")
    assert row["level"] == "ok" and row["days_left"] <= 30
    pace = [n for n in _forecasts(eligible) if "Rythme" in n.title]
    assert len(pace) == 1 and _forecasts(fleet_a).filter(title__contains="Rythme").count() == 1
    predictive.refresh_vehicle(car, notify=True)
    assert len([n for n in _forecasts(eligible) if "Rythme" in n.title]) == 1  # une fois par échéance
    assert MaintenancePlanEvent.objects.filter(schedule=plan, kind="pace").count() == 1


# =====================================================================================
# Intégration maintenance
# =====================================================================================


def test_completing_the_intervention_resets_the_plan_and_closes_the_alerts(api, active, car, eligible, fleet_a,
                                                                           sub_a):
    _steady(active)
    plan = _plan(car, "oil_change", done_km=2_200, done_days_ago=100)
    predictive.check_plans()
    assert _forecasts(eligible).count() == 1
    # Lire (« accuser réception de ») l'alerte ne vaut jamais réalisation.
    _forecasts(eligible).update(is_read=True, read_at=timezone.now())
    predictive.check_plans()
    plan.refresh_from_db()
    assert plan.alert_level == "alert" and plan.last_done_mileage == 2_200
    record = MaintenanceRecord.objects.create(subsidiary=sub_a, vehicle=car, maintenance_type=plan.maintenance_type,
                                              nature="periodic", status=MaintenanceStatus.PLANNED,
                                              scheduled_date=TODAY)
    plan.refresh_from_db()
    assert plan.last_done_mileage == 2_200  # planifiée : rien ne change
    api.force_authenticate(fleet_a)
    r = api.patch(f"/api/maintenance/{record.pk}/", {"status": "completed", "performed_date": str(TODAY),
                                                     "mileage": 11_980}, format="json")
    assert r.status_code == 200, r.content
    plan.refresh_from_db()
    assert plan.last_done_mileage == 11_980 and plan.last_done_date == TODAY and plan.last_record_id == record.pk
    assert plan.due_mileage == 21_980 and plan.next_date == TODAY + timedelta(days=365)
    assert plan.alert_level == ""
    assert _row(car, "oil_change")["level"] == "ok"
    kinds = list(MaintenancePlanEvent.objects.filter(schedule=plan).values_list("kind", flat=True))
    assert "done" in kinds and "cleared" in kinds
    predictive.check_plans()
    assert _forecasts(eligible).count() == 1  # aucune alerte obsolète
    # Une intervention plus ancienne que le dernier entretien ne fait pas reculer le plan.
    MaintenanceRecord.objects.create(subsidiary=sub_a, vehicle=car, maintenance_type=plan.maintenance_type,
                                     nature="periodic", status=MaintenanceStatus.COMPLETED,
                                     performed_date=TODAY - timedelta(days=30), mileage=10_500)
    plan.refresh_from_db()
    assert plan.last_done_mileage == 11_980


def test_a_general_service_feeds_the_revision_register(active, car, sub_a):
    from apps.vehicles.compliance import next_revision_km

    _steady(active)
    plan = _plan(car, "general_service")
    MaintenanceRecord.objects.create(subsidiary=sub_a, vehicle=car, maintenance_type=plan.maintenance_type,
                                     nature="periodic", status=MaintenanceStatus.COMPLETED, performed_date=TODAY,
                                     mileage=11_900, provider="Garage Plateau")
    assert VehicleRevision.objects.filter(vehicle=car, mileage_at_revision=11_900, date=TODAY).exists()
    car.refresh_from_db()
    assert next_revision_km(car) == 21_900
    row = _row(car, "general_service")
    assert row["due_mileage"] == 21_900 and row["last_done_mileage"] == 11_900
    # Une révision saisie directement au registre est reprise par le plan (pas de double saisie).
    VehicleRevision.objects.create(vehicle=car, date=TODAY, mileage_at_revision=11_950)
    assert _row(car, "general_service")["due_mileage"] == 21_950


def test_inspection_and_insurance_are_read_from_the_existing_registers(active, car, eligible):
    InsurancePolicy.objects.create(vehicle=car, company="Assur CI", expiry_date=TODAY + timedelta(days=5))
    TechnicalInspection.objects.create(vehicle=car, next_date=TODAY + timedelta(days=20),
                                       last_date=TODAY - timedelta(days=345))
    rows = {r["kind"]: r for r in predictive.vehicle_outlook(car)["plans"]}
    assert rows["insurance"]["level"] == "alert" and rows["insurance"]["source"] == "insurance"
    assert rows["technical_inspection"]["level"] == "ok" and rows["technical_inspection"]["days_left"] == 20
    assert not MaintenanceSchedule.objects.filter(maintenance_type__name__in=["Assurance", "Visite technique"]).exists()
    predictive.check_plans()
    assert not _forecasts(eligible).filter(title__icontains="assurance").exists()  # rappels existants seulement


# =====================================================================================
# API gestionnaire et périmètre
# =====================================================================================


def test_plan_api_respects_the_owner_subsidiary(api, active, car, fleet_a, sub_a, sub_b):
    _steady(active)
    plan = _plan(car, "oil_change", done_km=2_200, done_days_ago=100)
    api.force_authenticate(fleet_a)
    rows = api.get("/api/maintenance-plans/").json()["results"]
    assert any(r["id"] == str(plan.pk) and r["level"] == "alert" for r in rows)
    r = api.patch(f"/api/maintenance-plans/{plan.pk}/", {"interval_km": 5_000}, format="json")
    assert r.status_code == 200, r.content
    assert r.json()["due_mileage"] == 7_200 and r.json()["level"] == "overdue"
    assert MaintenancePlanEvent.objects.filter(schedule=plan, kind="config").exists()
    assert api.patch(f"/api/maintenance-plans/{plan.pk}/", {"last_done_date": str(TODAY + timedelta(days=2))},
                     format="json").status_code == 400
    events = api.get(f"/api/maintenance-plans/{plan.pk}/events/").json()
    assert events and events[0]["kind"] == "config"
    other = _user("pl-fleet-b2@test.io", RoleChoices.FLEET_MANAGER, sub_b)
    api.force_authenticate(other)
    assert all(r["id"] != str(plan.pk) for r in api.get("/api/maintenance-plans/").json()["results"])
    assert api.get(f"/api/maintenance-plans/{plan.pk}/").status_code == 404
    assert api.patch(f"/api/maintenance-plans/{plan.pk}/", {"interval_km": 9_000}, format="json").status_code == 404
    brakes = MaintenanceType.objects.get(kind="brakes")
    assert api.post("/api/maintenance-plans/", {"vehicle": str(car.pk), "maintenance_type": str(brakes.pk)},
                    format="json").status_code in (400, 403)
    api.force_authenticate(_user("pl-req@test.io", RoleChoices.REQUESTER, sub_a))
    assert api.get("/api/maintenance-plans/").status_code == 403
    auditor = _user("pl-aud@test.io", RoleChoices.AUDITOR, sub_a)
    api.force_authenticate(auditor)
    assert api.get("/api/maintenance-plans/").status_code == 200
    assert api.patch(f"/api/maintenance-plans/{plan.pk}/", {"interval_km": 9_000}, format="json").status_code == 403


def test_a_manager_adds_a_configurable_operation(api, sub_a, fleet_a):
    vehicle = _vehicle(sub_a, "CP-PL-OTHER")
    api.force_authenticate(fleet_a)
    r = api.post("/api/maintenance-types/", {"name": "Courroie de distribution", "kind": "other",
                                             "interval_km": 60_000, "interval_days": 1_825}, format="json")
    assert r.status_code == 201, r.content
    belt = r.json()["id"]
    predictive.ensure_default_types()
    assert api.post("/api/maintenance-types/", {"name": "Vidange bis", "kind": "oil_change"},
                    format="json").status_code == 400  # une seule opération de référence par nature
    r = api.post("/api/maintenance-plans/", {"vehicle": str(vehicle.pk), "maintenance_type": belt,
                                             "last_done_mileage": 1_000, "last_done_date": str(TODAY)}, format="json")
    assert r.status_code == 201, r.content
    assert r.json()["due_mileage"] == 61_000 and r.json()["next_date"] == (TODAY + timedelta(days=1_825)).isoformat()
    assert api.post("/api/maintenance-plans/", {"vehicle": str(vehicle.pk), "maintenance_type": belt},
                    format="json").status_code == 400  # un plan actif par opération


def test_manager_outlook_lists_watch_items_reliability_and_history(api, active, car, fleet_a, sub_b):
    _steady(active)
    _plan(car, "oil_change", done_km=1_000, done_days_ago=20)  # dépassé
    _plan(car, "brakes", done_km=9_900, done_days_ago=30)  # loin
    predictive.check_plans()
    api.force_authenticate(fleet_a)
    data = api.get("/api/maintenance-plans/outlook/").json()
    assert data["counts"]["overdue"] == 1 and data["watch"][0]["kind"] == "oil_change"
    forecast = next(f for f in data["forecasts"] if f["registration"] == car.registration)
    assert forecast["method"] == "weighted" and forecast["reliability"]["level"] == "good"
    assert any(e["kind"] == "alert" for e in data["events"])
    assert _no_money(data)
    api.force_authenticate(_user("pl-fleet-b3@test.io", RoleChoices.FLEET_MANAGER, sub_b))
    other = api.get("/api/maintenance-plans/outlook/").json()
    assert other["plans"] == 0 and other["watch"] == [] and other["events"] == []


def test_beneficiary_calendar_lists_only_his_vehicle(api, active, car, eligible, sub_a, km_policy, fleet_a,
                                                    fleet_admin, category):
    _steady(active)
    _plan(car, "oil_change", done_km=2_400, done_days_ago=100)
    colleague = _user("pl-coll@test.io", RoleChoices.REQUESTER, sub_a)
    CarPlanProfile.objects.create(user=colleague, category=category)
    other_car = _vehicle(sub_a, "CP-PL-COLL")
    running(km_policy, colleague, other_car, fleet_a, fleet_admin, days_ago=10)
    _plan(other_car, "brakes", done_km=1_000, done_days_ago=10)
    api.force_authenticate(eligible)
    data = api.get("/api/carplan/me/tracking/").json()
    assert {r["registration"] for r in data["maintenance"]} == {car.registration}
    assert data["next_operation"]["kind"] == "oil_change"
    api.force_authenticate(colleague)
    assert {r["registration"] for r in api.get("/api/carplan/me/tracking/").json()["maintenance"]} == \
        {other_car.registration}
    assert api.get("/api/maintenance-plans/").status_code == 403  # pas d'accès gestionnaire
