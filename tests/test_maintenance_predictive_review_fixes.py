"""Maintenance prédictive — non-régression des constats de la revue adverse.

Compteur remplacé (révision et interventions ramenées sur le compteur en place), intervention
annulée / supprimée / corrigée, intervention passée sans km, type « Vidange » du jeu de démo,
seuils démesurés, référentiel commun aux filiales, exécutions concurrentes, lectures en lot."""
import threading
from datetime import timedelta

import pytest
from django.db import IntegrityError, connection, connections, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from apps.carplan import mileage
from apps.carplan.models import CarPlanProfile
from apps.core.enums import MaintenanceStatus, NotificationType, RoleChoices
from apps.maintenance import predictive
from apps.maintenance.models import MaintenancePlanEvent, MaintenanceRecord, MaintenanceSchedule, MaintenanceType
from apps.notifications.models import Notification
from apps.vehicles import compliance
from apps.vehicles.models import Vehicle, VehicleRevision
from tests.test_carplan_c1 import _vehicle, category, eligible, employee, fleet_admin  # noqa: F401
from tests.test_carplan_mileage_tracking import active, api, car, km_policy, reading, running  # noqa: F401
from tests.test_finance_f1 import _user

pytestmark = pytest.mark.django_db

TODAY = timezone.localdate()
FORECAST = NotificationType.MAINTENANCE_FORECAST
DONE, CANCELLED = MaintenanceStatus.COMPLETED, MaintenanceStatus.CANCELLED


def _steady(a, *, per_day=50, start_km=10_000, days=(28, 21, 14, 7, 0.5)):
    """Relevés réguliers depuis la remise (40 jours) : compteur 11 975 aujourd'hui."""
    for d in days:
        reading(a, d, start_km + round(per_day * (40 - d)))


def _plan(vehicle, kind, *, done_km=None, done_days_ago=None):
    predictive.ensure_plans(vehicle)
    plan = MaintenanceSchedule.objects.get(vehicle=vehicle, maintenance_type__kind=kind, is_active=True)
    plan.last_done_mileage = done_km
    plan.last_done_date = TODAY - timedelta(days=done_days_ago) if done_days_ago is not None else None
    predictive.recompute_thresholds(plan, vehicle)
    plan.save()
    return plan


def _row(vehicle, kind):
    vehicle.refresh_from_db()
    return next(r for r in predictive.vehicle_outlook(vehicle)["plans"] if r["kind"] == kind)


def _record(vehicle, kind_or_type, *, days_ago=0, km=None, status=DONE, **extra):
    mtype = kind_or_type if isinstance(kind_or_type, MaintenanceType) else MaintenanceType.objects.get(kind=kind_or_type)
    return MaintenanceRecord.objects.create(vehicle=vehicle, maintenance_type=mtype, status=status,
                                            performed_date=TODAY - timedelta(days=days_ago), mileage=km,
                                            subsidiary=vehicle.subsidiary, **extra)


def _swap_meter(a, fleet_a, *, old=12_100, new=0, days_ago=5):
    mileage.replace_meter(a, actor=fleet_a, old_odometer=old, new_odometer=new, reason="Combiné remplacé",
                          recorded_at=timezone.now() - timedelta(days=days_ago))


# =====================================================================================
# Compteur remplacé : révision et interventions sur le compteur en place
# =====================================================================================


def test_a_revision_done_on_the_new_meter_becomes_the_last_one(active, car, fleet_a):
    """Constat 1 : après remplacement du compteur, la révision suivante (faite sur le nouveau
    compteur, à un kilométrage BRUT plus faible) doit primer — sinon blocage de conformité."""
    Vehicle.objects.filter(pk=car.pk).update(revision_interval_km=10_000)
    predictive.ensure_default_types()
    reading(active, 30, 11_000)
    VehicleRevision.objects.create(vehicle=car, date=TODAY - timedelta(days=30), mileage_at_revision=11_000)
    reading(active, 10, 12_000)
    _swap_meter(active, fleet_a)
    reading(active, 2, 500)
    car.refresh_from_db()
    assert compliance.next_revision_km(car) == 11_000 - 12_100 + 10_000  # ancienne révision, reportée
    _record(car, "general_service", km=500)
    assert VehicleRevision.objects.filter(vehicle=car, mileage_at_revision=500, date=TODAY).exists()
    assert compliance.last_revision(car).mileage_at_revision == 500
    assert compliance.next_revision_km(car) == 10_500
    reading(active, 0.2, 9_000)
    car.refresh_from_db()
    assert "revision_overdue" not in [i["code"] for i in compliance.compliance_issues(car)]
    row = _row(car, "general_service")
    assert row["due_mileage"] == 10_500 and row["level"] != "overdue"


def test_a_revision_entered_in_the_register_after_a_meter_swap_feeds_the_plan(active, car, fleet_a):
    """Constat 1 (sens registre → plan) : la révision saisie au registre sur le nouveau compteur
    devient le dernier entretien du plan « Révision générale »."""
    Vehicle.objects.filter(pk=car.pk).update(revision_interval_km=10_000)
    reading(active, 30, 11_000)
    VehicleRevision.objects.create(vehicle=car, date=TODAY - timedelta(days=30), mileage_at_revision=11_000)
    reading(active, 10, 12_000)
    _swap_meter(active, fleet_a)
    reading(active, 2, 9_000)
    VehicleRevision.objects.create(vehicle=car, date=TODAY - timedelta(days=1), mileage_at_revision=8_900)
    row = _row(car, "general_service")
    assert row["last_done_date"] == (TODAY - timedelta(days=1)).isoformat() and row["last_done_mileage"] == 8_900
    assert row["due_mileage"] == 18_900 and row["level"] == "ok"
    assert compliance.next_revision_km(car) == 18_900


def test_a_meter_swap_on_the_same_day_is_settled_by_the_value_read():
    day = TODAY - timedelta(days=3)
    shifts = [(day, 12_100, 0)]
    assert mileage.shift_since(shifts, day - timedelta(days=1)) == 12_100  # lu avant : ancien compteur
    assert mileage.shift_since(shifts, day + timedelta(days=1)) == 0  # lu après : nouveau compteur
    assert mileage.shift_since(shifts, day, km=12_080) == 12_100  # le jour même, proche de l'ancien
    assert mileage.shift_since(shifts, day, km=40) == 0  # le jour même, proche de la nouvelle base


def test_an_intervention_done_before_a_meter_swap_and_closed_after_is_moved_to_the_new_meter(
        api, active, car, fleet_a):
    """Constat 2 : vidange à 12 050 km (ancien compteur) il y a 8 jours, compteur 12 100 → 0 il y a
    5 jours, clôture aujourd'hui : seuil 12 050 − 12 100 + 10 000 = 9 950 sur le compteur en place."""
    reading(active, 30, 11_000)
    reading(active, 10, 12_000)
    _swap_meter(active, fleet_a)
    reading(active, 2, 500)
    _record(car, "oil_change", days_ago=8, km=12_050)
    plan = MaintenanceSchedule.objects.get(vehicle=car, maintenance_type__kind="oil_change")
    assert plan.last_done_mileage == -50 and plan.due_mileage == 9_950
    assert _row(car, "oil_change")["remaining_km"] == 9_450
    assert MaintenancePlanEvent.objects.filter(schedule=plan, kind="done", message__contains="12050 km lus").exists()
    # Saisie manuelle du dernier entretien (km de la facture, lu sur l'ancien compteur) : même report.
    api.force_authenticate(fleet_a)
    filters = MaintenanceSchedule.objects.get(vehicle=car, maintenance_type__kind="filters")
    r = api.patch(f"/api/maintenance-plans/{filters.pk}/", {"last_done_mileage": 11_100,
                                                            "last_done_date": str(TODAY - timedelta(days=20))},
                  format="json")
    assert r.status_code == 200, r.content
    filters.refresh_from_db()
    assert filters.last_done_mileage == -1_000 and filters.due_mileage == 19_000


def test_a_reference_plan_created_after_a_meter_swap_uses_the_current_meter(sub_a, active, car, fleet_a):
    predictive.ensure_default_types()
    reading(active, 30, 11_000)
    _record(car, "oil_change", days_ago=30, km=11_000)
    MaintenanceSchedule.objects.filter(vehicle=car).delete()  # véhicule devenu Car Plan plus tard
    reading(active, 10, 12_000)
    _swap_meter(active, fleet_a)
    reading(active, 2, 500)
    car.refresh_from_db()
    predictive.ensure_plans(car)
    plan = MaintenanceSchedule.objects.get(vehicle=car, maintenance_type__kind="oil_change")
    assert plan.last_done_mileage == -1_100 and plan.due_mileage == 8_900


# =====================================================================================
# Cycle de vie de l'intervention : annulée, supprimée, corrigée
# =====================================================================================


def test_a_cancelled_or_deleted_intervention_no_longer_counts_as_done(active, car):
    """Constat 4 : seule une intervention TERMINÉE vaut réalisation — annulée ou supprimée, le
    plan revient à l'entretien précédent et la révision inscrite quitte le registre."""
    _steady(active)
    car.refresh_from_db()
    oil = _plan(car, "oil_change", done_km=2_400, done_days_ago=100)  # préavis, saisi à la main
    rec = _record(car, "oil_change", km=11_975)
    oil.refresh_from_db()
    assert oil.last_record_id == rec.pk and oil.due_mileage == 21_975
    rec.status = CANCELLED
    rec.save()
    oil.refresh_from_db()
    assert oil.last_done_date == TODAY - timedelta(days=100) and oil.last_done_mileage == 2_400
    assert oil.last_record_id is None and oil.due_mileage == 12_400
    assert _row(car, "oil_change")["level"] == "notice"
    assert MaintenancePlanEvent.objects.filter(schedule=oil, kind="undone").exists()
    # Remise à « terminée » : de nouveau prise en compte.
    rec.status = DONE
    rec.save()
    oil.refresh_from_db()
    assert oil.last_record_id == rec.pk and oil.due_mileage == 21_975
    # Révision générale terminée puis supprimée : le registre et la conformité l'oublient.
    gs = _record(car, "general_service", km=11_975)
    assert VehicleRevision.objects.filter(vehicle=car, mileage_at_revision=11_975).exists()
    gs.delete()
    assert not VehicleRevision.objects.filter(vehicle=car, mileage_at_revision=11_975).exists()
    assert compliance.next_revision_km(car) == compliance.interval_for(car)
    plan = MaintenanceSchedule.objects.get(vehicle=car, maintenance_type__kind="general_service")
    assert plan.last_record_id is None and plan.last_done_date is None


def test_a_cancelled_intervention_falls_back_to_the_previous_completed_one(active, car):
    _steady(active)
    car.refresh_from_db()
    predictive.ensure_plans(car)
    first = _record(car, "brakes", days_ago=20, km=11_000)
    second = _record(car, "brakes", km=11_975)
    second.status = CANCELLED
    second.save()
    plan = MaintenanceSchedule.objects.get(vehicle=car, maintenance_type__kind="brakes")
    assert plan.last_record_id == first.pk and plan.last_done_mileage == 11_000
    assert plan.last_done_date == TODAY - timedelta(days=20) and plan.due_mileage == 41_000


def test_correcting_a_completed_intervention_resyncs_the_plan(active, car):
    _steady(active)
    car.refresh_from_db()
    predictive.ensure_plans(car)
    rec = _record(car, "tyres", days_ago=1, km=11_900)
    rec.mileage = 11_950
    rec.save()
    plan = MaintenanceSchedule.objects.get(vehicle=car, maintenance_type__kind="tyres")
    assert plan.last_done_mileage == 11_950 and plan.due_mileage == 51_950 and plan.last_record_id == rec.pk


def test_a_revision_carrying_a_cost_is_kept_and_flagged(active, car):
    _steady(active)
    car.refresh_from_db()
    predictive.ensure_plans(car)
    rec = _record(car, "general_service", km=11_975)
    VehicleRevision.objects.filter(vehicle=car, mileage_at_revision=11_975).update(cost=50_000)
    rec.status = CANCELLED
    rec.save()
    assert VehicleRevision.objects.filter(vehicle=car, mileage_at_revision=11_975).exists()
    assert MaintenancePlanEvent.objects.filter(kind="undone", message__contains="conservée au registre").exists()


# =====================================================================================
# Intervention passée sans km : jamais le compteur du jour
# =====================================================================================


def test_a_past_intervention_without_mileage_takes_the_reading_of_its_date(active, car, sub_a):
    """Constat 3 : vidange du jour J−28 sans km → relevé de ce jour-là (10 600), pas le compteur
    du jour (11 975) ; sans relevé à cette date, le km reste inconnu (aucun seuil inventé)."""
    _steady(active)
    car.refresh_from_db()
    _plan(car, "oil_change", done_km=2_000, done_days_ago=200)
    _record(car, "oil_change", days_ago=28)
    plan = MaintenanceSchedule.objects.get(vehicle=car, maintenance_type__kind="oil_change")
    assert plan.last_done_date == TODAY - timedelta(days=28)
    assert plan.last_done_mileage == 10_600 and plan.due_mileage == 20_600
    other = _vehicle(sub_a, "CP-RV-NOKM")
    Vehicle.objects.filter(pk=other.pk).update(mileage=30_000)
    oil = MaintenanceType.objects.get(kind="oil_change")
    MaintenanceSchedule.objects.filter(vehicle=other).delete()
    MaintenanceSchedule.objects.create(vehicle=other, maintenance_type=oil, due_mileage=5_000)
    _record(other, "oil_change", days_ago=28)
    plan = MaintenanceSchedule.objects.get(vehicle=other, maintenance_type=oil)
    assert plan.last_done_mileage is None and plan.due_mileage is None
    assert plan.next_date == TODAY - timedelta(days=28) + timedelta(days=365)
    assert MaintenancePlanEvent.objects.filter(schedule=plan, message__contains="kilométrage inconnu").exists()


# =====================================================================================
# Type « Vidange » du jeu de démonstration
# =====================================================================================


def test_the_demo_oil_change_type_is_adopted_and_ignored_on_an_electric_vehicle(sub_a):
    """Constat 7 : le type « Vidange » (sans nature) devient l'opération de référence thermique."""
    vidange = MaintenanceType.objects.create(name="Vidange", interval_km=10_000, interval_days=180)
    ev, diesel = _vehicle(sub_a, "EV-RV-01"), _vehicle(sub_a, "TH-RV-01")
    Vehicle.objects.filter(pk=ev.pk).update(fuel_type="electric", mileage=5_000)
    Vehicle.objects.filter(pk=diesel.pk).update(mileage=5_000)
    ev.refresh_from_db()
    diesel.refresh_from_db()
    predictive.ensure_plans(ev)
    vidange.refresh_from_db()
    assert vidange.kind == "oil_change" and vidange.combustion_only and vidange.interval_days == 180
    assert not MaintenanceType.objects.filter(name="Vidange moteur").exists()
    for vehicle in (ev, diesel):
        _record(vehicle, vidange, km=5_000)
    assert not MaintenanceSchedule.objects.filter(vehicle=ev, maintenance_type=vidange).exists()
    assert all(r["kind"] != "oil_change" for r in predictive.vehicle_outlook(ev)["plans"])
    assert MaintenanceSchedule.objects.filter(vehicle=diesel, maintenance_type=vidange).count() == 1


def test_a_legacy_untyped_oil_change_feeds_the_reference_plan(sub_a):
    predictive.ensure_default_types()  # « Vidange moteur » existe déjà
    legacy = MaintenanceType.objects.create(name="Vidange", interval_km=10_000, interval_days=180)
    ev, diesel = _vehicle(sub_a, "EV-RV-02"), _vehicle(sub_a, "TH-RV-02")
    Vehicle.objects.filter(pk=ev.pk).update(fuel_type="electric")
    ev.refresh_from_db()
    predictive.ensure_plans(diesel)
    for vehicle in (ev, diesel):
        _record(vehicle, legacy, km=1_000)
    oil_plans = MaintenanceSchedule.objects.filter(vehicle=diesel, maintenance_type__in=MaintenanceType.objects.filter(
        name__in=["Vidange", "Vidange moteur"]))
    assert oil_plans.count() == 1 and oil_plans.get().maintenance_type.kind == "oil_change"
    assert oil_plans.get().last_done_mileage == 1_000
    assert not MaintenanceSchedule.objects.filter(vehicle=ev, maintenance_type__name__in=["Vidange",
                                                                                          "Vidange moteur"]).exists()


# =====================================================================================
# Seuils démesurés
# =====================================================================================


def test_an_oversized_threshold_is_refused_and_never_breaks_the_forecasts(active, car, fleet_a, eligible,
                                                                           company_admin, monkeypatch):
    """Constat 5 : seuil saisi démesuré refusé (400) ; une valeur aberrante déjà en base ne fait
    ni tomber les listes ni taire les alertes des autres plans du véhicule."""
    _steady(active)
    car.refresh_from_db()
    _plan(car, "brakes", done_km=-30_000, done_days_ago=100)  # dépassé : doit alerter
    tyres = MaintenanceSchedule.objects.get(vehicle=car, maintenance_type__kind="tyres")
    client = APIClient(raise_request_exception=False)
    client.force_authenticate(fleet_a)
    assert client.patch(f"/api/maintenance-plans/{tyres.pk}/", {"due_mileage": 2_000_000_000},
                        format="json").status_code == 400
    tyres.refresh_from_db()
    assert tyres.due_mileage is None
    MaintenanceSchedule.objects.filter(pk=tyres.pk).update(due_mileage=2_000_000_000)
    MaintenanceType.objects.filter(kind="filters").update(interval_days=1_000_000_000)
    r = client.get("/api/maintenance-plans/")
    assert r.status_code == 200
    row = next(x for x in r.json()["results"] if x["id"] == str(tyres.pk))
    assert row["forecast_km_date"] is None and "hors de portée" in row["forecast_label"]
    client.force_authenticate(eligible)
    assert client.get("/api/carplan/me/tracking/").status_code == 200
    assert predictive.check_plans()["vehicles"] >= 1
    assert Notification.objects.filter(recipient=fleet_a, notification_type=FORECAST,
                                       title__startswith="Freinage").exists()
    client.force_authenticate(company_admin)
    oil = MaintenanceType.objects.get(kind="oil_change")
    assert client.patch(f"/api/maintenance-types/{oil.pk}/", {"interval_days": 1_000_000_000},
                        format="json").status_code == 400
    # Écriture atomique : un échec après l'enregistrement n'en laisse rien.
    client.force_authenticate(fleet_a)

    def boom(*args, **kwargs):
        raise RuntimeError("panne simulée")

    monkeypatch.setattr(predictive, "refresh_vehicle", boom)
    brakes = MaintenanceSchedule.objects.get(vehicle=car, maintenance_type__kind="brakes")
    assert client.patch(f"/api/maintenance-plans/{brakes.pk}/", {"interval_km": 9_000},
                        format="json").status_code == 500
    brakes.refresh_from_db()
    assert brakes.interval_km is None


# =====================================================================================
# Référentiel commun : une filiale ne règle pas les seuils du groupe
# =====================================================================================


def test_a_subsidiary_cannot_change_the_group_reference_thresholds(api, active, car, eligible, fleet_a, sub_b,
                                                                  company_admin):
    """Constat 8 : le gestionnaire d'une filiale sœur ne peut plus faire taire les pré-alertes."""
    _steady(active)
    car.refresh_from_db()
    plan = _plan(car, "oil_change", done_km=2_400, done_days_ago=100)  # préavis
    oil = MaintenanceType.objects.get(kind="oil_change")
    fleet_b = _user("rv-t1-fleet-b@test.io", RoleChoices.FLEET_MANAGER, sub_b)
    silence = {"notice_days": 0, "alert_days": 0, "urgent_days": 0, "interval_km": 900_000}
    for user in (fleet_b, fleet_a):
        api.force_authenticate(user)
        assert api.patch(f"/api/maintenance-types/{oil.pk}/", silence, format="json").status_code == 403
        assert api.delete(f"/api/maintenance-types/{oil.pk}/").status_code == 403
    oil.refresh_from_db()
    assert oil.notice_days == 14 and oil.interval_km == 10_000
    api.force_authenticate(fleet_b)
    assert api.post("/api/maintenance-types/", {"name": "Lavage B", "kind": "other"}, format="json").status_code == 201
    MaintenanceType.objects.filter(kind="tyres").update(kind="")  # opération de référence absente
    assert api.post("/api/maintenance-types/", {"name": "Pneus B", "kind": "tyres"},
                    format="json").status_code == 403
    assert _row(car, "oil_change")["level"] == "notice"
    predictive.check_plans()
    assert Notification.objects.filter(recipient=eligible, notification_type=FORECAST).count() == 1
    # La filiale ajuste SES véhicules par la dérogation du plan ; le groupe règle le référentiel.
    api.force_authenticate(fleet_a)
    assert api.patch(f"/api/maintenance-plans/{plan.pk}/", {"interval_km": 12_000}, format="json").status_code == 200
    api.force_authenticate(company_admin)
    assert api.patch(f"/api/maintenance-types/{oil.pk}/", {"notice_days": 21}, format="json").status_code == 200


# =====================================================================================
# Concurrence
# =====================================================================================


def _in_parallel(fn, n=2):
    errors = []

    def run():
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=run) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors


def _rendezvous(monkeypatch, module, name, parties=2):
    """Les fils se retrouvent au premier appel de `name` : la fenêtre de course est garantie."""
    barrier, seen, real = threading.Barrier(parties, timeout=10), threading.local(), getattr(module, name)

    def wrapper(*args, **kwargs):
        if not getattr(seen, "done", False):
            seen.done = True
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, wrapper)


@pytest.mark.django_db(transaction=True)
def test_two_simultaneous_refreshes_send_a_single_alert(monkeypatch, active, car, eligible, fleet_a):
    """Constat 9 : tâche et recalcul après relevé au même instant → une seule alerte."""
    _steady(active)
    car.refresh_from_db()
    plan = _plan(car, "oil_change", done_km=1_000, done_days_ago=20)  # dépassé
    _rendezvous(monkeypatch, predictive, "is_carplan_vehicle")
    _in_parallel(lambda: predictive.refresh_vehicle(car, notify=True))
    assert Notification.objects.filter(recipient=fleet_a, notification_type=FORECAST).count() == 1
    assert MaintenancePlanEvent.objects.filter(schedule=plan, kind="alert").count() == 1


@pytest.mark.django_db(transaction=True)
def test_simultaneous_first_reads_never_duplicate_the_reference_plans(monkeypatch, active, car):
    """Constat 10 : un seul plan actif par véhicule et par opération, même en concurrence."""
    predictive.ensure_default_types()
    MaintenanceSchedule.objects.filter(vehicle=car).delete()
    _rendezvous(monkeypatch, predictive, "applies")
    _in_parallel(lambda: predictive.ensure_plans(car))
    kinds = list(MaintenanceSchedule.objects.filter(vehicle=car).values_list("maintenance_type__kind", flat=True))
    assert sorted(kinds) == sorted({"oil_change", "filters", "tyres", "brakes", "general_service"})


def test_the_database_refuses_a_second_active_plan(api, active, car, fleet_a):
    predictive.ensure_plans(car)
    oil = MaintenanceType.objects.get(kind="oil_change")
    with pytest.raises(IntegrityError), transaction.atomic():
        MaintenanceSchedule.objects.create(vehicle=car, maintenance_type=oil)
    old = MaintenanceSchedule.objects.create(vehicle=car, maintenance_type=oil, is_active=False)
    api.force_authenticate(fleet_a)
    assert api.patch(f"/api/maintenance-plans/{old.pk}/", {"is_active": True}, format="json").status_code == 400


# =====================================================================================
# Lectures en lot, sans écriture, paginées
# =====================================================================================


def _add_fleet(n, start, km_policy, category, fleet_a, fleet_admin, sub_a):  # noqa: F811
    for i in range(start, start + n):
        user = _user(f"rv-perf-{i}@test.io", RoleChoices.REQUESTER, sub_a)
        CarPlanProfile.objects.create(user=user, category=category)
        a = running(km_policy, user, _vehicle(sub_a, f"RV-PERF-{i:02d}"), fleet_a, fleet_admin)
        for d in (21, 14, 7, 1):
            reading(a, d, 10_000 + 50 * (40 - d))


def _measure(api, url):
    with CaptureQueriesContext(connection) as q:
        r = api.get(url)
    assert r.status_code == 200, (url, r.content)
    writes = [x["sql"] for x in q.captured_queries
              if x["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
    return len(q), writes, r.json()


def test_manager_lists_read_in_bulk_without_writing(api, km_policy, category, fleet_a, fleet_admin, sub_a):
    """Constat 12 : suivi kilométrique, tableau de bord et liste des plans lus EN LOT (coût en
    requêtes indépendant du nombre de véhicules), sans aucune écriture, listes paginées."""
    urls = ("/api/carplan/mileage-followup/", "/api/maintenance-plans/outlook/", "/api/maintenance-plans/")
    _add_fleet(2, 0, km_policy, category, fleet_a, fleet_admin, sub_a)
    api.force_authenticate(fleet_a)
    small = {url: _measure(api, url) for url in urls}
    _add_fleet(6, 2, km_policy, category, fleet_a, fleet_admin, sub_a)
    large = {url: _measure(api, url) for url in urls}
    for url in urls:
        assert not small[url][1] and not large[url][1], (url, large[url][1])
        assert large[url][0] - small[url][0] <= 2, (url, small[url][0], large[url][0])
    assert large["/api/carplan/mileage-followup/"][2]["count"] == 8
    page = api.get("/api/maintenance-plans/", {"page_size": 10, "page": 2}).json()
    assert page["count"] == 40 and page["page"] == 2 and len(page["results"]) == 10
    # Tâche périodique : un second passage sans changement n'écrit plus les plans.
    predictive.check_plans()
    with CaptureQueriesContext(connection) as q:
        predictive.check_plans()
    sql = [x["sql"].lstrip().upper() for x in q.captured_queries]
    assert not [x for x in sql if x.startswith("UPDATE") and "MAINTENANCE_MAINTENANCESCHEDULE" in x]
    assert len([x for x in sql if not x.startswith(("SAVEPOINT", "RELEASE SAVEPOINT"))]) / 8 <= 14


def test_reference_plans_are_created_when_the_vehicle_enters_car_plan(active, car):
    kinds = set(MaintenanceSchedule.objects.filter(vehicle=car).values_list("maintenance_type__kind", flat=True))
    assert kinds == {"oil_change", "filters", "tyres", "brakes", "general_service"}
