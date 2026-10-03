"""Suivi kilométrique Car Plan — non-régression des constats de la revue adverse : corrections
en rafale (délai depuis la déclaration d'origine, plafond, alertes sans rebond), rappels espacés
après un long silence, suivi gestionnaire filtré et paginé."""
from datetime import timedelta

import django.utils.timezone
import pytest
from django.utils import timezone

from apps.carplan import mileage, operations
from apps.carplan.models import CarPlanProfile, MileageReading
from apps.carplan.services import CarPlanError
from apps.core.enums import NotificationType, RoleChoices
from apps.maintenance import predictive
from apps.maintenance.models import MaintenanceSchedule
from apps.notifications.models import Notification
from tests.test_carplan_c1 import _vehicle, category, eligible, employee, fleet_admin  # noqa: F401
from tests.test_carplan_mileage_tracking import active, api, car, km_policy, reading, running  # noqa: F401
from tests.test_finance_f1 import _user

pytestmark = pytest.mark.django_db

TODAY = timezone.localdate()
FORECAST = NotificationType.MAINTENANCE_FORECAST
READING_DUE = NotificationType.MILEAGE_READING_DUE


def _steady(a, *, per_day=50, start_km=10_000, days=(28, 21, 14, 7, 0.5)):
    for d in days:
        reading(a, d, start_km + round(per_day * (40 - d)))


def _oil_threshold(vehicle, threshold):
    predictive.ensure_plans(vehicle)
    plan = MaintenanceSchedule.objects.get(vehicle=vehicle, maintenance_type__kind="oil_change")
    plan.last_done_mileage, plan.last_done_date = threshold - 10_000, TODAY - timedelta(days=100)
    predictive.recompute_thresholds(plan, vehicle)
    plan.save()
    return plan


# =====================================================================================
# Corrections en rafale
# =====================================================================================


def test_flip_flopping_corrections_never_spam_the_managers(api, active, car, eligible, fleet_a, company_admin,
                                                         monkeypatch):
    """Constat 5 : même sans plafond de corrections, un niveau qui oscille autour du seuil ne
    renotifie pas — une alerte par niveau atteint et par échéance."""
    monkeypatch.setattr(mileage, "MAX_SELF_CORRECTIONS", 100)
    _steady(active)
    car.refresh_from_db()
    _oil_threshold(car, 12_600)
    api.force_authenticate(eligible)
    r = api.post("/api/carplan/me/mileage/", {"odometer": 12_000}, format="json")
    assert r.status_code == 201, r.content
    current = r.json()["id"]
    for _ in range(4):
        for km in (12_650, 12_000):
            r = api.post(f"/api/carplan/me/mileage/{current}/correct/", {"odometer": km}, format="json")
            assert r.status_code == 201, r.content
            current = r.json()["id"]
    overdue = Notification.objects.filter(notification_type=FORECAST, title__contains="dépassée")
    assert overdue.filter(recipient=fleet_a).count() == 1
    assert overdue.filter(recipient=company_admin).count() == 1
    assert overdue.filter(recipient=eligible).count() == 1
    assert Notification.objects.filter(recipient=eligible, notification_type=FORECAST).count() <= 2


def test_the_beneficiary_correction_window_runs_from_the_original_declaration(active, car, eligible, monkeypatch):
    """Constat 5 : chaque correction devenait la « dernière déclaration » et rouvrait le délai de
    48 h ; le délai court désormais depuis la déclaration d'origine, deux corrections au plus."""
    reading(active, 3, 10_200)
    declared = operations.declare_mileage(active, actor=eligible, odometer=10_900)
    real_now = timezone.now()
    monkeypatch.setattr(django.utils.timezone, "now", lambda: real_now + timedelta(hours=40))
    first = mileage.correct_reading(declared, actor=eligible, odometer=10_500)
    monkeypatch.setattr(django.utils.timezone, "now", lambda: real_now + timedelta(hours=50))
    with pytest.raises(CarPlanError, match="48 h après la déclaration"):
        mileage.correct_reading(first, actor=eligible, odometer=10_450)
    assert mileage.correctable_reading(active, eligible) is None
    monkeypatch.setattr(django.utils.timezone, "now", lambda: real_now)
    second = mileage.correct_reading(first, actor=eligible, odometer=10_450)
    with pytest.raises(CarPlanError, match="déjà été corrigée 2 fois"):
        mileage.correct_reading(second, actor=eligible, odometer=10_400)
    assert mileage.correctable_reading(active, eligible) is None
    assert MileageReading.objects.filter(assignment=active).order_by("-recorded_at", "-id").first().odometer == 10_450


def test_the_api_refuses_a_third_self_correction(api, active, eligible):
    reading(active, 3, 10_200)
    api.force_authenticate(eligible)
    current = api.post("/api/carplan/me/mileage/", {"odometer": 10_900}, format="json").json()["id"]
    for km in (10_500, 10_450):
        r = api.post(f"/api/carplan/me/mileage/{current}/correct/", {"odometer": km}, format="json")
        assert r.status_code == 201, r.content
        current = r.json()["id"]
    r = api.post(f"/api/carplan/me/mileage/{current}/correct/", {"odometer": 10_400}, format="json")
    assert r.status_code == 400 and "gestionnaire" in str(r.content.decode())
    assert api.get("/api/carplan/me/tracking/").json()["correctable_reading"] is None


# =====================================================================================
# Rappels espacés
# =====================================================================================


def test_a_first_run_after_a_long_silence_sends_the_reminder_only(active, eligible, fleet_a):
    """Constat 11 : dernier relevé il y a 20 jours (fréquence 7) — premier passage : le rappel
    seul ; relance trois jours plus tard, signalement aux gestionnaires quatre jours après."""
    reading(active, 20, 10_500)
    mine = Notification.objects.filter(recipient=eligible, notification_type=READING_DUE)
    to_managers = Notification.objects.filter(recipient=fleet_a, title__contains="relevé kilométrique en retard")
    assert mileage.check_reading_reminders(TODAY) == {"reminders": 1, "relaunches": 0, "escalations": 0}
    assert mine.count() == 1 and to_managers.count() == 0
    assert mileage.check_reading_reminders(TODAY + timedelta(days=2)) == {"reminders": 0, "relaunches": 0,
                                                                          "escalations": 0}
    assert mileage.check_reading_reminders(TODAY + timedelta(days=3))["relaunches"] == 1
    assert mileage.check_reading_reminders(TODAY + timedelta(days=6))["escalations"] == 0
    assert mileage.check_reading_reminders(TODAY + timedelta(days=7))["escalations"] == 1
    assert mileage.check_reading_reminders(TODAY + timedelta(days=8)) == {"reminders": 0, "relaunches": 0,
                                                                          "escalations": 0}
    assert mine.count() == 2 and to_managers.count() == 1


# =====================================================================================
# Suivi gestionnaire : filtre et pagination côté serveur
# =====================================================================================


def test_the_followup_is_filtered_counted_and_paginated_by_the_server(api, km_policy, eligible, car, fleet_a,
                                                                      fleet_admin, sub_a, category):  # noqa: F811
    stale = running(km_policy, eligible, car, fleet_a, fleet_admin, days_ago=20)
    for i in range(2):
        user = _user(f"rv-fu-{i}@test.io", RoleChoices.REQUESTER, sub_a)
        CarPlanProfile.objects.create(user=user, category=category)
        fresh = running(km_policy, user, _vehicle(sub_a, f"RV-FU-{i}"), fleet_a, fleet_admin, days_ago=10)
        reading(fresh, 1, 10_500)
    api.force_authenticate(fleet_a)
    data = api.get("/api/carplan/mileage-followup/", {"page_size": 2}).json()
    assert data["count"] == 3 and len(data["results"]) == 2 and data["page"] == 1
    assert data["counts"]["total"] == 3 and data["counts"]["late"] == 1
    assert data["results"][0]["reference"] == stale.reference and data["results"][0]["last_reminder"] is None
    assert len(api.get("/api/carplan/mileage-followup/", {"page_size": 2, "page": 2}).json()["results"]) == 1
    late = api.get("/api/carplan/mileage-followup/", {"state": "late"}).json()
    assert late["count"] == 1 and [r["reference"] for r in late["results"]] == [stale.reference]
    assert late["counts"]["total"] == 3  # compteurs sur tout le périmètre, filtre ou non
    mileage.check_reading_reminders(TODAY)
    row = next(r for r in api.get("/api/carplan/mileage-followup/").json()["results"]
               if r["reference"] == stale.reference)
    assert row["last_reminder"]["step"] == "reading_due"
