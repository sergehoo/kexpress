"""Barème kilométrique des courses — historisation, sélection par date, instantané figé.

Ce qui est protégé :
1. Le tarif applicable se choisit sur la date PRÉVUE de chaque segment : un aller le 31/10 et
   un retour le 01/11 ne sont pas valorisés au même tarif, même réservation.
2. Deux barèmes actifs d'un même périmètre ne couvrent jamais un même jour — refusé par l'API
   avec un message clair, et par la base en dernier recours.
3. Un tarif n'est jamais écrasé : un barème qui a servi ne change plus de montant, et une
   course clôturée garde son coût quoi qu'il arrive ensuite au barème.
4. « Non valorisée » n'est pas « gratuite » : sans barème, le coût vaut None, jamais 0.
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone
from hypothesis import given
from hypothesis import strategies as st
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.enums import ReservationStatus, RoleChoices, TripType
from apps.finance import pricing
from apps.finance.models import TripPricing, TripPricingRule

# Dates lointaines : le test ne dépend pas du jour où il tourne.
SEP_1, OCT_31, NOV_1, DEC_31 = date(2030, 9, 1), date(2030, 10, 31), date(2030, 11, 1), date(2030, 12, 31)


def _rule(id_, amount, start, end=None, **kw):
    return pricing.Rule(id=id_, version=1, amount_per_km=Decimal(amount), currency="XOF",
                        valid_from=start, valid_until=end, **kw)


# --- Cœur pur ------------------------------------------------------------------


def test_spec_example_seven_km_at_two_hundred():
    assert pricing.distance_cost(Decimal("7"), Decimal("200")) == Decimal("1400.00")
    assert pricing.distance_cost(Decimal("7.8"), Decimal("200")) == Decimal("1560.00")
    assert pricing.variance(Decimal("1400.00"), Decimal("1560.00")) == Decimal("160.00")


def test_unknown_distance_or_tariff_is_none_not_zero():
    assert pricing.distance_cost(None, Decimal("200")) is None
    assert pricing.distance_cost(Decimal("7"), None) is None


def test_money_is_rounded_half_up_to_the_cent():
    assert pricing.distance_cost(Decimal("0.005"), Decimal("1")) == Decimal("0.01")


def test_rule_selection_follows_the_calendar_with_inclusive_bounds():
    rules = [_rule(1, "175", date(2030, 1, 1), date(2030, 8, 31)),
             _rule(2, "200", SEP_1, OCT_31),
             _rule(3, "225", NOV_1, DEC_31)]
    assert pricing.select_rule(rules, day=date(2030, 9, 15)).amount_per_km == Decimal("200")
    assert pricing.select_rule(rules, day=OCT_31).amount_per_km == Decimal("200")
    assert pricing.select_rule(rules, day=NOV_1).amount_per_km == Decimal("225")
    assert pricing.select_rule(rules, day=date(2031, 1, 1)) is None


def test_inactive_rules_are_ignored():
    rules = [_rule(1, "200", SEP_1, OCT_31, active=False)]
    assert pricing.select_rule(rules, day=date(2030, 9, 15)) is None


def test_advanced_scopes_are_ignored_until_enabled():
    rules = [_rule(1, "200", SEP_1),
             _rule(2, "260", SEP_1, scope=pricing.SUBSIDIARY, subsidiary_id="abj")]
    day = date(2030, 9, 15)
    assert pricing.select_rule(rules, day=day, subsidiary_id="abj").amount_per_km == Decimal("200")
    chosen = pricing.select_rule(rules, day=day, subsidiary_id="abj", advanced=True)
    assert chosen.amount_per_km == Decimal("260"), "le plus précis l'emporte une fois activé"
    other = pricing.select_rule(rules, day=day, subsidiary_id="dkr", advanced=True)
    assert other.amount_per_km == Decimal("200"), "une autre filiale retombe sur le global"


DATES = st.dates(min_value=date(2020, 1, 1), max_value=date(2040, 1, 1))


@given(DATES, st.one_of(st.none(), DATES), DATES, st.one_of(st.none(), DATES))
def test_overlap_is_symmetric_and_matches_day_by_day_intersection(a1, a2, b1, b2):
    """PROPRIÉTÉ — la règle de chevauchement égale l'intersection « à la main »."""
    if a2 is not None and a2 < a1:
        a1, a2 = a2, a1
    if b2 is not None and b2 < b1:
        b1, b2 = b2, b1
    got = pricing.periods_overlap(a1, a2, b1, b2)
    assert got == pricing.periods_overlap(b1, b2, a1, a2)
    expected = max(a1, b1) <= min(a2 or date.max, b2 or date.max)
    assert got == expected


def test_pooling_impact_matches_the_spec_example():
    """Courses A (16 km) + B (12 km), détour 3 km : 28 km séparées → 19 km ensemble."""
    impact = pricing.pooling_impact([Decimal("16"), Decimal("12")], 3, Decimal("200"))
    assert impact["km_separate"] == Decimal("28.00")
    assert impact["km_grouped"] == Decimal("19.00")
    assert impact["km_avoided"] == Decimal("9.00")
    assert impact["cost_separate"] == Decimal("5600.00")
    assert impact["cost_grouped"] == Decimal("3800.00")
    assert impact["saving"] == Decimal("1800.00")


def test_pooling_never_claims_a_negative_saving():
    impact = pricing.pooling_impact([Decimal("5"), Decimal("5")], 40, Decimal("200"))
    assert impact["km_avoided"] == Decimal("0.00") and impact["saving"] == Decimal("0.00")


# --- Base : non-chevauchement --------------------------------------------------


def _db_rule(start, end, amount="200", **kw):
    return TripPricingRule.objects.create(name=f"B {start}", amount_per_km=Decimal(amount),
                                          valid_from=start, valid_until=end, reason="test", **kw)


@pytest.mark.django_db
def test_database_refuses_two_overlapping_active_global_rules():
    _db_rule(SEP_1, OCT_31)
    with pytest.raises(IntegrityError), transaction.atomic():
        _db_rule(date(2030, 10, 15), date(2030, 11, 30), amount="220")


@pytest.mark.django_db
def test_database_accepts_adjacent_periods_and_inactive_overlaps():
    _db_rule(SEP_1, OCT_31)
    _db_rule(NOV_1, None, amount="225")                     # lendemain de la fin : OK
    _db_rule(date(2030, 10, 1), DEC_31, amount="999", active=False)  # inactif : OK
    assert TripPricingRule.objects.count() == 3


@pytest.mark.django_db
def test_an_open_ended_rule_blocks_any_later_rule():
    _db_rule(SEP_1, None)
    with pytest.raises(IntegrityError), transaction.atomic():
        _db_rule(date(2031, 6, 1), None, amount="300")


# --- API : gestion des barèmes ---------------------------------------------------


@pytest.fixture
def api():
    return APIClient()


def _payload(**kw):
    base = {"name": "Sept-Oct 2030", "amount_per_km": "200", "valid_from": SEP_1.isoformat(),
            "valid_until": OCT_31.isoformat(), "reason": "Hausse du carburant"}
    base.update(kw)
    return base


@pytest.mark.django_db
def test_company_admin_creates_a_rule_and_the_change_is_journaled(api, company_admin):
    from apps.audit.models import AuditLog

    api.force_authenticate(company_admin)
    response = api.post(reverse("trip-pricing-rule-list"), _payload(), format="json")
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["version"] == 1 and body["created_by"] == str(company_admin.pk)
    log = AuditLog.objects.filter(changes__action="trip_pricing_rule").last()
    assert log is not None and log.changes["reason"] == "Hausse du carburant"
    assert log.changes["amount_per_km"] == "200.00"


@pytest.mark.django_db
def test_overlap_is_refused_with_a_message_naming_the_other_rule(api, company_admin):
    api.force_authenticate(company_admin)
    api.post(reverse("trip-pricing-rule-list"), _payload(), format="json")
    response = api.post(reverse("trip-pricing-rule-list"),
                        _payload(name="Oct-Nov", amount_per_km="220",
                                 valid_from="2030-10-15", valid_until="2030-11-30"),
                        format="json")
    assert response.status_code == 400
    assert "Sept-Oct 2030" in response.content.decode()


@pytest.mark.django_db
def test_reason_is_mandatory(api, company_admin):
    api.force_authenticate(company_admin)
    response = api.post(reverse("trip-pricing-rule-list"), _payload(reason=""), format="json")
    assert response.status_code == 400 and "reason" in response.json()


@pytest.mark.django_db
def test_advanced_scope_is_refused_until_enabled(api, company_admin, sub_a):
    api.force_authenticate(company_admin)
    response = api.post(reverse("trip-pricing-rule-list"),
                        _payload(scope="subsidiary", subsidiary=str(sub_a.pk)), format="json")
    assert response.status_code == 400 and "scope" in response.json()


@pytest.mark.django_db
@pytest.mark.parametrize("role,can_read,can_write", [
    (RoleChoices.FLEET_MANAGER, True, False),
    (RoleChoices.SUBSIDIARY_ADMIN, True, False),
    (RoleChoices.FINANCE, True, True),       # financier groupe (sans filiale)
    (RoleChoices.DEPARTMENT_MANAGER, False, False),
    (RoleChoices.REQUESTER, False, False),
    (RoleChoices.DRIVER, False, False),
])
def test_rule_permissions_by_role(api, sub_a, role, can_read, can_write):
    subsidiary = None if role == RoleChoices.FINANCE else sub_a
    user = User.objects.create_user(f"tp-{role}@test.io", "pw", role=role, subsidiary=subsidiary)
    api.force_authenticate(user)
    assert (api.get(reverse("trip-pricing-rule-list")).status_code == 200) is can_read
    created = api.post(reverse("trip-pricing-rule-list"), _payload(), format="json")
    assert (created.status_code == 201) is can_write, created.content[:200]


@pytest.mark.django_db
def test_subsidiary_finance_cannot_change_the_group_wide_tariff(api, sub_a):
    """Le barème global vaut pour toutes les filiales : il se décide au niveau groupe."""
    user = User.objects.create_user("tp-fin-a@test.io", "pw", role=RoleChoices.FINANCE, subsidiary=sub_a)
    api.force_authenticate(user)
    assert api.post(reverse("trip-pricing-rule-list"), _payload(), format="json").status_code == 403


@pytest.mark.django_db
def test_rules_cannot_be_deleted(api, company_admin):
    api.force_authenticate(company_admin)
    rule_id = api.post(reverse("trip-pricing-rule-list"), _payload(), format="json").json()["id"]
    response = api.delete(reverse("trip-pricing-rule-detail", args=[rule_id]))
    assert response.status_code == 405
    assert TripPricingRule.objects.filter(pk=rule_id).exists()


# --- Cycle de vie de l'instantané -------------------------------------------------


def _local(day, hour):
    return timezone.make_aware(datetime.combine(day, time(hour, 0)), timezone.get_current_timezone())


def _round_trip(sub, requester, *, out_day, back_day):
    from apps.reservations.models import Reservation
    from apps.reservations.services import _ensure_trips

    dep, ret = _local(out_day, 17), _local(back_day, 9)
    res = Reservation.objects.create(
        subsidiary=sub, requester=requester, created_by=requester, trip_date=out_day,
        departure_time=dep, return_time=ret, estimated_return=ret + timedelta(hours=2),
        origin="Cocody", destination="Plateau", purpose="Mission", passengers=2,
        needs_driver=False, trip_type=TripType.ROUND_TRIP, status=ReservationStatus.APPROVED,
    )
    return {trip.leg: trip for trip in _ensure_trips(res)}


def _one_way(sub, requester, *, day, hour):
    from apps.reservations.models import Reservation
    from apps.reservations.services import _ensure_trips

    dep = _local(day, hour)
    res = Reservation.objects.create(
        subsidiary=sub, requester=requester, created_by=requester, trip_date=day,
        departure_time=dep, estimated_return=dep + timedelta(hours=1), origin="Cocody",
        destination=f"Plateau {hour}", purpose="Mission", passengers=2, needs_driver=False,
        trip_type=TripType.ONE_WAY, status=ReservationStatus.APPROVED,
    )
    return _ensure_trips(res)[0]


def _route(trip, km):
    from apps.tracking.models import TripRoute

    TripRoute.objects.update_or_create(trip=trip, defaults={
        "origin_label": "Cocody", "destination_label": "Plateau",
        "planned_distance_km": Decimal(str(km)),
    })
    trip.refresh_from_db()


@pytest.fixture
def two_periods(db):
    _db_rule(SEP_1, OCT_31, amount="200")
    _db_rule(NOV_1, DEC_31, amount="225")


@pytest.mark.django_db
def test_each_leg_of_a_round_trip_uses_the_tariff_of_its_own_date(two_periods, sub_a, requester_a):
    """Spec §4 : aller 31/10 → 7 × 200 = 1 400 ; retour 01/11 → 7 × 225 = 1 575."""
    from apps.finance.trip_pricing import refresh_estimate

    legs = _round_trip(sub_a, requester_a, out_day=OCT_31, back_day=NOV_1)
    # Les courses naissent tarifées (barème connu), sans coût tant que la distance manque.
    assert TripPricing.objects.get(trip=legs["outbound"]).amount_per_km == Decimal("200")
    assert TripPricing.objects.get(trip=legs["outbound"]).estimated_cost is None

    for trip in legs.values():
        _route(trip, 7)
        refresh_estimate(trip)
    assert TripPricing.objects.get(trip=legs["outbound"]).estimated_cost == Decimal("1400.00")
    assert TripPricing.objects.get(trip=legs["return"]).estimated_cost == Decimal("1575.00")


@pytest.mark.django_db
def test_trip_without_applicable_tariff_is_unpriced_not_free(sub_a, requester_a):
    from apps.finance.trip_pricing import refresh_estimate

    legs = _round_trip(sub_a, requester_a, out_day=date(2031, 3, 1), back_day=date(2031, 3, 2))
    _route(legs["outbound"], 7)
    refresh_estimate(legs["outbound"])
    snapshot = TripPricing.objects.get(trip=legs["outbound"])
    assert snapshot.rule_id is None and snapshot.estimated_cost is None


@pytest.fixture
def driven(two_periods, sub_a, requester_a, fleet_a, vehicle_a, monkeypatch):
    """Aller du 15/09 : estimé sur 7 km, réalisé sur 7,8 km mesurés au GPS, puis clôturé."""
    from apps.finance.trip_pricing import refresh_estimate
    from apps.trips import services as trip_services
    from apps.trips.models import Trip

    legs = _round_trip(sub_a, requester_a, out_day=date(2030, 9, 15), back_day=date(2030, 9, 16))
    trip = legs["outbound"]
    Trip.objects.filter(pk=trip.pk).update(vehicle=vehicle_a)
    trip.refresh_from_db()
    _route(trip, 7)
    refresh_estimate(trip)

    monkeypatch.setattr("apps.tracking.live.real_traveled_km", lambda _trip: 7.8)
    trip_services.start_trip(trip, fleet_a, start_mileage=1000)
    trip_services.end_trip(trip, fleet_a, end_mileage=1008)
    return trip


@pytest.mark.django_db
def test_actual_cost_uses_the_retained_gps_distance_at_the_estimate_tariff(driven):
    """Spec §6 : estimé 1 400, réel 7,8 km × 200 = 1 560, écart +160."""
    snapshot = TripPricing.objects.get(trip=driven)
    assert snapshot.estimated_cost == Decimal("1400.00")
    assert snapshot.actual_distance_km == Decimal("7.80")
    assert snapshot.actual_distance_source == "gps"
    assert snapshot.actual_cost == Decimal("1560.00")
    assert snapshot.variance == Decimal("160.00")
    assert snapshot.frozen_at is None, "revenue n'est pas clôturée : pas encore figée"


@pytest.mark.django_db
def test_closed_trip_keeps_its_cost_whatever_happens_to_the_tariff(driven, fleet_a, monkeypatch):
    """Spec §5 : une modification ultérieure ne touche jamais une course clôturée."""
    from apps.finance.trip_pricing import freeze, record_actual, refresh_estimate
    from apps.trips import services as trip_services

    trip_services.close_trip(driven, fleet_a)
    frozen = TripPricing.objects.get(trip=driven)
    assert frozen.frozen_at is not None

    # Tout ce qui alimente le calcul bouge après la clôture : le barème de septembre est
    # remplacé, et la mesure GPS « change » (données tardives, recalcul, correction).
    TripPricingRule.objects.filter(valid_from=SEP_1).update(active=False)
    _db_rule(SEP_1, OCT_31, amount="999")
    monkeypatch.setattr("apps.tracking.live.real_traveled_km", lambda _trip: 42.0)
    for call in (refresh_estimate, record_actual, freeze):
        call(driven)

    after = TripPricing.objects.get(trip=driven)
    assert (after.amount_per_km, after.actual_distance_km, after.actual_cost, after.estimated_cost) == (
        Decimal("200.00"), Decimal("7.80"), Decimal("1560.00"), Decimal("1400.00"))
    assert after.frozen_at == frozen.frozen_at, "la date de gel ne se réécrit pas non plus"


@pytest.mark.django_db
def test_a_used_rule_cannot_change_its_amount_but_can_close_its_period(
    api, company_admin, driven, fleet_a
):
    from apps.trips import services as trip_services

    trip_services.close_trip(driven, fleet_a)
    rule = TripPricingRule.objects.get(valid_from=SEP_1)
    api.force_authenticate(company_admin)
    url = reverse("trip-pricing-rule-detail", args=[rule.pk])

    response = api.patch(url, {"amount_per_km": "250", "reason": "correction"}, format="json")
    assert response.status_code == 400 and "amount_per_km" in response.json()

    response = api.patch(url, {"valid_until": "2030-09-30", "reason": "fin anticipée"}, format="json")
    assert response.status_code == 200, response.content
    assert response.json()["version"] == 2

    too_early = api.patch(url, {"valid_until": "2030-09-10", "reason": "trop tôt"}, format="json")
    assert too_early.status_code == 400, "des courses clôturées ont été valorisées au 15/09"


@pytest.mark.django_db
def test_creating_a_rule_prices_the_planned_trips_it_covers(api, company_admin, sub_a, requester_a):
    """Un barème créé après la planification valorise aussitôt les courses concernées : le
    dispatching n'attend pas."""
    legs = _round_trip(sub_a, requester_a, out_day=date(2031, 5, 10), back_day=date(2031, 5, 11))
    _route(legs["outbound"], 10)
    api.force_authenticate(company_admin)
    response = api.post(reverse("trip-pricing-rule-list"),
                        _payload(name="Mai 2031", valid_from="2031-05-01", valid_until="2031-05-31",
                                 amount_per_km="210"), format="json")
    assert response.status_code == 201
    assert TripPricing.objects.get(trip=legs["outbound"]).estimated_cost == Decimal("2100.00")


@pytest.mark.django_db
def test_trip_pricing_endpoint_serves_the_snapshot_to_cost_viewers_only(
    api, driven, fleet_a, requester_a
):
    url = reverse("trip-pricing", args=[driven.pk])
    api.force_authenticate(fleet_a)
    body = api.get(url).json()
    assert body["priced"] is True and body["actual_cost"] == "1560.00" and body["variance"] == "160.00"
    api.force_authenticate(requester_a)
    assert api.get(url).status_code == 403


# --- Dispatching et statistiques (§10, §14) ----------------------------------------


@pytest.mark.django_db
def test_board_shows_the_estimated_cost_to_cost_viewers(api, fleet_a, sub_a, requester_a):
    """Spec §10 : « Distance 12,4 km · Tarif 200 FCFA/km · Coût estimé 2 480 FCFA »."""
    from apps.finance.trip_pricing import refresh_estimate

    today = timezone.localdate()
    _db_rule(today - timedelta(days=1), None, amount="200")
    legs = _round_trip(sub_a, requester_a, out_day=today, back_day=today + timedelta(days=1))
    trip = legs["outbound"]
    from apps.trips.models import Trip
    Trip.objects.filter(pk=trip.pk).update(planned_departure_at=timezone.now() + timedelta(hours=2))
    trip.refresh_from_db()
    _route(trip, "12.4")
    refresh_estimate(trip)

    api.force_authenticate(fleet_a)
    rows = api.get(reverse("dispatch-board"), {"hours": "72"}).json()["trips"]
    row = next(r for r in rows if r["id"] == str(trip.pk))
    assert Decimal(row["distance_km"]) == Decimal("12.4")
    assert row["pricing"]["amount_per_km"] == "200.00"
    assert row["pricing"]["estimated_cost"] == "2480.00"


@pytest.mark.django_db
def test_grouping_suggestion_carries_its_financial_impact(api, fleet_a, sub_a, requester_a):
    from apps.dispatch.models import DispatchSuggestion

    today = timezone.localdate()
    _db_rule(today - timedelta(days=1), None, amount="200")
    first = _one_way(sub_a, requester_a, day=today, hour=9)
    second = _one_way(sub_a, requester_a, day=today, hour=10)
    _route(first, 16)
    _route(second, 12)
    DispatchSuggestion.objects.create(kind="group", generated_for=sub_a, rationale="A + B",
                                      payload={"trip_ids": [str(first.pk), str(second.pk)]},
                                      metrics={"detour_km": 3, "distance_source": "road"})
    api.force_authenticate(fleet_a)
    body = api.get(reverse("dispatch-suggestion-list")).json()
    row = (body["results"] if isinstance(body, dict) else body)[0]
    impact = row["financial_impact"]
    assert (impact["km_separate"], impact["km_grouped"], impact["km_avoided"]) == ("28.00", "19.00", "9.00")
    assert (impact["cost_separate"], impact["cost_grouped"], impact["saving"]) == ("5600.00", "3800.00", "1800.00")


@pytest.mark.django_db
def test_stats_report_the_realised_cost_and_keep_estimates_apart(api, driven, fleet_a, requester_a):
    from apps.trips.models import Trip

    # Réalisée le jour prévu (le test la fait partir « maintenant » : on la date réellement).
    Trip.objects.filter(pk=driven.pk).update(actual_departure=_local(date(2030, 9, 15), 10))
    api.force_authenticate(fleet_a)
    params = {"period": "custom", "start": "2030-09-01", "end": "2030-09-30"}
    body = api.get(reverse("trip-cost-stats"), params).json()
    assert body["realised"]["total_cost"] == "1560.00"
    assert body["realised"]["avg_cost_per_km"] == "200.00"
    # Le retour (16/09) est encore planifié : estimé à part, jamais ajouté au réel.
    assert body["planned"]["trips"] == 1
    assert body["by_vehicle"][0]["label"] == "A-100"

    api.force_authenticate(requester_a)
    assert api.get(reverse("trip-cost-stats"), params).status_code == 403


@pytest.mark.django_db
def test_straight_line_detour_is_corrected_before_being_added_to_road_legs(
    api, fleet_a, sub_a, requester_a
):
    """Routage indisponible : un détour de 3 km à vol d'oiseau en vaut 3,9 par la route
    (facteur 1,3). L'additionner tel quel à des trajets routiers gonflerait l'économie."""
    from apps.dispatch.models import DispatchSuggestion

    today = timezone.localdate()
    _db_rule(today - timedelta(days=1), None, amount="200")
    first = _one_way(sub_a, requester_a, day=today, hour=9)
    second = _one_way(sub_a, requester_a, day=today, hour=10)
    _route(first, 16)
    _route(second, 12)
    DispatchSuggestion.objects.create(kind="group", generated_for=sub_a, rationale="A + B",
                                      payload={"trip_ids": [str(first.pk), str(second.pk)]},
                                      metrics={"detour_km": 3, "distance_source": "straight_line"})
    api.force_authenticate(fleet_a)
    body = api.get(reverse("dispatch-suggestion-list")).json()
    impact = (body["results"] if isinstance(body, dict) else body)[0]["financial_impact"]
    assert impact["km_grouped"] == "19.90" and impact["km_avoided"] == "8.10"
    assert impact["approximate"] is True
    assert impact["saving"] == "1620.00"


@pytest.mark.django_db
def test_each_trip_is_valued_at_its_own_tariff_in_the_separate_scenario(
    api, fleet_a, sub_a, requester_a
):
    """Deux courses de part et d'autre d'un changement de barème : « sans mutualisation »
    doit égaler la somme des coûts affichés course par course."""
    from apps.dispatch.models import DispatchSuggestion

    today = timezone.localdate()
    tomorrow = today + timedelta(days=1)
    _db_rule(today - timedelta(days=1), today, amount="200")
    _db_rule(tomorrow, None, amount="250")
    first = _one_way(sub_a, requester_a, day=today, hour=23)
    second = _one_way(sub_a, requester_a, day=tomorrow, hour=0)
    _route(first, 10)
    _route(second, 10)
    DispatchSuggestion.objects.create(kind="group", generated_for=sub_a, rationale="A + B",
                                      payload={"trip_ids": [str(first.pk), str(second.pk)]},
                                      metrics={"detour_km": 0, "distance_source": "road"})
    api.force_authenticate(fleet_a)
    body = api.get(reverse("dispatch-suggestion-list")).json()
    impact = (body["results"] if isinstance(body, dict) else body)[0]["financial_impact"]
    assert impact["cost_separate"] == "4500.00"     # 10 × 200 + 10 × 250
    assert impact["cost_grouped"] == "2000.00"      # 10 km au tarif de la course de tête
    assert impact["saving"] == "2500.00"
