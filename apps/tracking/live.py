"""Service temps réel : routage routier (OSRM), itinéraire prévu vs réel,
distance / progression / vitesse calculées depuis les positions GPS RÉELLES.

Aucune donnée n'est simulée : les positions proviennent des appareils
(PWA chauffeur/demandeur via l'endpoint d'ingestion) ou d'un boîtier télématique.
Sortie déjà sérialisable JSON. Partagé entre le consumer WebSocket et l'API REST.
"""
from __future__ import annotations

import logging
import math
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

logger = logging.getLogger("apps.tracking.live")

# Rapport distance routière / distance à vol d'oiseau (approx. urbain/interurbain CI).
# Sert d'estimation de la distance restante quand l'itinéraire routier n'est pas calculable.
ROAD_WINDING_FACTOR = 1.3
# En deçà de ce rayon (à vol d'oiseau, km), la course est considérée arrivée → ETA 0.
ARRIVAL_RADIUS_KM = 0.08
# Vitesse instantanée minimale jugée fiable pour l'ETA. En deçà (arrêt, ralenti transitoire,
# bruit GPS), on retombe sur la vitesse de référence pour éviter une ETA aberrante
# (un feu rouge ne doit pas faire exploser le temps restant).
ETA_REF_MIN_SPEED_KMH = 5.0

from apps.tracking.models import TripLocationPoint, TripTrackingSession, VehicleLocation
from apps.tracking.osrm import road_route
from apps.trips.models import Trip
from apps.vehicles.models import Vehicle

# Au-delà de ce délai sans nouvelle position GPS, la vitesse est considérée inconnue.
STALE_AFTER_S = 180
# Deux envois espacés de moins de ce délai sont ignorés (anti-spam appareil).
MIN_INTERVAL_S = 3
# Au-delà de cette vitesse implicite entre deux points, on considère un saut GPS
# (perte de signal, recalage) : segment exclu de la distance, vitesse inconnue.
MAX_PLAUSIBLE_KMH = 160

# --- Détection d'anomalies (#12) -----------------------------------------
# Écart maxi toléré (m) à l'itinéraire prévu avant alerte de détour.
DEVIATION_THRESHOLD_M = 1000
# Immobilité (≤ STOP_SPEED_KMH) au-delà de cette durée = arrêt anormal.
STOP_MIN_MINUTES = 15
STOP_SPEED_KMH = 3
# Anti-spam : un même type d'alerte n'est pas recréé avant ce délai.
ALERT_DEDUP_MINUTES = 15


# --- Géométrie ----------------------------------------------------------


def _haversine(a, b):
    """Distance en km entre [lat,lng] a et b."""
    r = 6371.0
    dlat = math.radians(b[0] - a[0])
    dlng = math.radians(b[1] - a[1])
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(a[0])) * math.cos(math.radians(b[0])) * math.sin(dlng / 2) ** 2
    )
    # min(1.0, …) : garde-fou numérique (arrondi flottant pour points quasi-antipodaux).
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def _point_to_polyline_m(lat, lng, polyline) -> float | None:
    """Distance minimale (m) d'un point aux sommets de l'itinéraire (approximation)."""
    if not polyline:
        return None
    return min(_haversine([lat, lng], [float(p[0]), float(p[1])]) for p in polyline) * 1000.0


def _notify_managers_anomaly(trip, ntype, title, message):
    from apps.notifications.events import managers_of
    from apps.notifications.services import notify_many

    notify_many(
        managers_of(trip.subsidiary_id), ntype,
        title=title, message=message, link=f"/trips/{trip.id}", severity="warning",
    )


def push_trip_update(trip_id) -> None:
    """Pousse l'instantané de suivi d'une course aux clients WebSocket (meilleur effort).

    Sans couche de canaux configurée (tests, contexte synchrone), c'est un no-op silencieux.
    """
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        from apps.tracking.consumers import trip_group

        layer = get_channel_layer()
        if layer is None:
            return
        payload = trip_tracking(None, trip_id)
        if payload:
            async_to_sync(layer.group_send)(
                trip_group(str(trip_id)), {"type": "trip.update", "payload": payload}
            )
    except Exception:
        pass


def recalculate_route(trip, lat, lng, now=None) -> dict | None:
    """#3C — Recalcule l'itinéraire OSRM depuis la position courante vers la destination.

    Met à jour le tracé/ETA actifs de la course, notifie le chauffeur et pousse la mise
    à jour aux clients WebSocket. Renvoie un résumé du recalcul, ou None si non applicable
    (pas d'itinéraire ou destination inconnue).
    """
    from apps.core.enums import NotificationType
    from apps.notifications.services import notify

    now = now or timezone.now()
    route = getattr(trip, "route", None)
    if route is None or route.destination_lat is None or route.destination_lng is None:
        return None

    dest = (float(route.destination_lat), float(route.destination_lng))
    result = road_route([(lat, lng), dest])
    geometry = result.get("geometry") or []
    distance_km = result.get("distance_km")
    if distance_km is None:
        distance_km = round(_haversine([lat, lng], list(dest)), 2)
    duration_min = result.get("duration_min")
    if not duration_min:
        duration_min = max(1, round(distance_km / AVG_SPEED_KMH * 60))

    route.active_geometry = geometry
    route.active_distance_km = Decimal(str(round(distance_km, 1)))
    route.active_duration_min = int(round(duration_min))
    route.reroute_count = (route.reroute_count or 0) + 1
    route.last_rerouted_at = now
    route.save(update_fields=[
        "active_geometry", "active_distance_km", "active_duration_min",
        "reroute_count", "last_rerouted_at", "updated_at",
    ])

    # Notifier le chauffeur (s'il a un compte) du nouvel itinéraire et de l'ETA.
    driver = getattr(trip, "driver", None)
    if driver and driver.user_id:
        notify(
            driver.user,
            NotificationType.ROUTE_RECALCULATED,
            title=f"Itinéraire recalculé — {trip.destination}",
            message=(
                f"Détour détecté : nouvel itinéraire calculé depuis votre position. "
                f"Distance restante ~{route.active_distance_km} km, "
                f"arrivée estimée ~{route.active_duration_min} min."
            ),
            link=f"/trips/{trip.id}",
            severity="warning",
        )

    push_trip_update(trip.id)
    return {
        "reroute_count": route.reroute_count,
        "distance_km": float(route.active_distance_km),
        "duration_min": route.active_duration_min,
        "geometry": geometry,
    }


def detect_anomalies(trip, lat, lng, speed_kmh, now=None) -> list[str]:
    """#12 — Détecte détour (écart à l'itinéraire prévu) et arrêt anormal, et alerte
    les gestionnaires (dédupliqué). Retourne la liste des anomalies déclenchées.

    L'alerte de détour porte un `deviation_m` ; l'arrêt anormal a `deviation_m=NULL`
    (marqueur), ce qui permet de dédupliquer chaque type indépendamment sans nouveau modèle.
    """
    from apps.core.enums import NotificationType
    from apps.tracking.models import RouteDeviationAlert

    now = now or timezone.now()
    created: list[str] = []
    dedup_since = now - timedelta(minutes=ALERT_DEDUP_MINUTES)

    # --- Détour : position trop éloignée de l'itinéraire suivi ---
    route = getattr(trip, "route", None)
    geometry = route.current_geometry() if route else None
    if geometry:
        off_m = _point_to_polyline_m(lat, lng, geometry)
        if off_m is not None and off_m > DEVIATION_THRESHOLD_M:
            already = RouteDeviationAlert.objects.filter(
                trip=trip, deviation_m__isnull=False, occurred_at__gte=dedup_since,
            ).exists()
            if not already:
                RouteDeviationAlert.objects.create(
                    trip=trip, severity="warning", deviation_m=Decimal(str(round(off_m, 2))),
                    latitude=Decimal(str(round(lat, 6))), longitude=Decimal(str(round(lng, 6))),
                    occurred_at=now,
                )
                _notify_managers_anomaly(
                    trip, NotificationType.ROUTE_DEVIATION,
                    f"Véhicule hors itinéraire — {trip.vehicle.registration}",
                    f"Course « {trip.destination} » : écart d'environ "
                    f"{round(off_m / 1000, 1)} km par rapport à l'itinéraire prévu.",
                )
                created.append("deviation")
                # #3C — recalcul auto de l'itinéraire depuis la position courante.
                try:
                    recalculate_route(trip, lat, lng, now)
                except Exception:
                    pass

    # --- Arrêt anormal : immobilité prolongée (hors saut GPS) ---
    if speed_kmh is not None and float(speed_kmh) <= STOP_SPEED_KMH:
        session = _get_session(trip)
        pts = session.points.order_by("recorded_at")
        last_moving = pts.filter(speed_kmh__gt=STOP_SPEED_KMH).last()
        first = pts.first()
        # Immobile depuis le dernier point en mouvement (ou le 1er point observé).
        since = last_moving.recorded_at if last_moving else (first.recorded_at if first else None)
        stationary_long = since is not None and (now - since).total_seconds() >= STOP_MIN_MINUTES * 60
        if stationary_long:
            already = RouteDeviationAlert.objects.filter(
                trip=trip, deviation_m__isnull=True, occurred_at__gte=dedup_since,
            ).exists()
            if not already:
                RouteDeviationAlert.objects.create(
                    trip=trip, severity="warning", deviation_m=None,
                    latitude=Decimal(str(round(lat, 6))), longitude=Decimal(str(round(lng, 6))),
                    occurred_at=now,
                )
                _notify_managers_anomaly(
                    trip, NotificationType.OTHER,
                    f"Arrêt prolongé — {trip.vehicle.registration}",
                    f"Course « {trip.destination} » : véhicule à l'arrêt depuis plus de "
                    f"{STOP_MIN_MINUTES} minutes.",
                )
                created.append("stop")

    return created


def ensure_geometry(route):
    """Calcule et met en cache le tracé routier (OSRM) si nécessaire."""
    if route.geometry:
        return route.geometry
    pts = []
    if route.origin_lat is not None:
        pts.append((float(route.origin_lat), float(route.origin_lng)))
    for wp in route.waypoints.order_by("order"):
        if wp.latitude is not None:
            pts.append((float(wp.latitude), float(wp.longitude)))
    if route.destination_lat is not None:
        pts.append((float(route.destination_lat), float(route.destination_lng)))

    result = road_route(pts)
    route.geometry = result["geometry"]
    if result["distance_km"]:
        route.planned_distance_km = Decimal(str(result["distance_km"]))
    if result["duration_min"]:
        route.planned_duration_min = int(result["duration_min"])
    # Estimation carburant (moteur apprenant) pour la course planifiée.
    try:
        from apps.fuelintel.engine import estimate_fuel

        trip = route.trip
        est = estimate_fuel(
            float(route.planned_distance_km or 0),
            vehicle=trip.vehicle, driver=trip.driver,
            subsidiary_id=trip.subsidiary_id,
            departure_time=trip.actual_departure,
        )
        route.estimated_fuel_l = est["liters"]
    except Exception:
        pass
    route.save(update_fields=[
        "geometry", "planned_distance_km", "planned_duration_min",
        "estimated_fuel_l", "updated_at",
    ])
    return route.geometry


def ensure_trip_route(trip, allow_provision=True):
    """Provisionne un `TripRoute` exploitable (coordonnées + distance/durée/tracé) pour la
    course. Le flux de réservation ne crée pas d'itinéraire ; on le fabrique ici à la volée
    en géocodant l'origine/destination de la réservation, puis en calculant l'itinéraire
    (OSRM, avec repli à vol d'oiseau si OSRM est indisponible).

    Idempotent (ne refait rien si l'itinéraire est déjà complet) et tolérant aux pannes
    (géocodage / OSRM) : ne lève jamais. Renvoie le `TripRoute` ou None (destination
    non géocodable). Indispensable pour l'ETA / distance restante / progression du suivi.

    `allow_provision=False` : aucun appel réseau (géocodage/OSRM). Utilisé par le diffuseur
    temps réel (boucle sur toute la flotte) pour ne jamais bloquer le fan-out sur une requête
    Nominatim lente — le provisionnement se fait via les vues par course (WS/REST).
    """
    from django.conf import settings

    from apps.tracking.models import TripRoute

    route = getattr(trip, "route", None)
    if route is not None and route.planned_distance_km and route.destination_lat is not None:
        return route  # déjà complet : aucun appel réseau
    # Provisionnement réseau non sollicité (diffuseur flotte) ou désactivé (tests / dégradé).
    if not allow_provision or not getattr(settings, "TRACKING_GEOCODE_ROUTES", True):
        return route
    try:
        if route is None:
            from apps.core.enums import TripLeg
            from apps.maps.geocoding import geocode_one

            res = getattr(trip, "reservation", None)
            # `trip.destination` est déjà la destination du SEGMENT (aller ou retour).
            # Sur le retour, l'origine du trajet est la destination de l'aller.
            dest_label = (trip.destination or (res.destination if res else "") or "").strip()
            if res and trip.leg == TripLeg.RETURN:
                origin_label = (res.destination or "").strip()
            else:
                origin_label = ((res.origin if res else "") or "").strip()
            dest = geocode_one(dest_label)
            if not dest:
                return None  # destination introuvable : pas d'itinéraire possible
            origin = geocode_one(origin_label) if origin_label else None
            # get_or_create : tolère une création concurrente (TripRoute.trip est OneToOne)
            # — savepoint interne + renvoi de l'itinéraire « gagnant » au lieu de planter.
            route, _ = TripRoute.objects.get_or_create(
                trip=trip,
                defaults=dict(
                    origin_label=origin_label[:255],
                    origin_lat=origin[0] if origin else None,
                    origin_lng=origin[1] if origin else None,
                    destination_label=dest_label[:255],
                    destination_lat=dest[0],
                    destination_lng=dest[1],
                ),
            )
        if not route.geometry:
            ensure_geometry(route)  # une seule fois : évite de rappeler OSRM à chaque poll
        # Repli à vol d'oiseau si OSRM n'a pas fourni de distance (évite de recalculer
        # à chaque rafraîchissement et garantit une ETA exploitable hors ligne).
        if not route.planned_distance_km and route.origin_lat is not None and route.destination_lat is not None:
            km = round(_haversine(
                [float(route.origin_lat), float(route.origin_lng)],
                [float(route.destination_lat), float(route.destination_lng)],
            ) * ROAD_WINDING_FACTOR, 1)
            if km > 0:
                route.planned_distance_km = Decimal(str(km))
                route.planned_duration_min = max(1, round(km / AVG_SPEED_KMH * 60))
                route.save(update_fields=["planned_distance_km", "planned_duration_min", "updated_at"])
        return route
    except Exception:
        logger.warning("ensure_trip_route: échec pour trip=%s", getattr(trip, "pk", None), exc_info=True)
        return TripRoute.objects.filter(trip=trip).first()  # refetch DB (pas le cache périmé)


def _live_metrics(rt: dict, loc, speed) -> dict:
    """ETA / distance restante / parcourue / progression du suivi temps réel.

    La distance restante privilégie la mesure « live » (position GPS courante → destination),
    qui reflète la réalité et gère proprement l'arrivée — y compris quand la distance parcourue
    cumulée sous-estime le trajet (segments GPS aberrants exclus). Repli sur (prévu − parcouru)
    si la position courante est inconnue. `remaining_km` et `eta_min` valent None (et non 0)
    quand aucune référence n'est disponible, pour ne pas faire croire à une arrivée.
    """
    planned = float(rt.get("distance_km") or 0.0)
    traveled = float(rt.get("traveled_km") or 0.0)
    dest = rt.get("destination_point")
    cur = None
    if loc is not None and getattr(loc, "latitude", None) is not None and loc.longitude is not None:
        cur = [float(loc.latitude), float(loc.longitude)]

    remaining = None
    arrived = False
    if cur and dest:
        crow = _haversine(cur, dest)  # distance à vol d'oiseau (km)
        if crow <= ARRIVAL_RADIUS_KM:
            remaining, arrived = 0.0, True
        else:
            # Facteur de sinuosité au-delà du dernier km ; en approche la ligne droite suffit.
            remaining = round(crow * (ROAD_WINDING_FACTOR if crow > 1.0 else 1.0), 2)
    elif planned > 0:
        remaining = round(max(0.0, planned - traveled), 2)
        arrived = remaining <= ARRIVAL_RADIUS_KM

    # Progression : fraction accomplie sur une base de distance cohérente.
    total = planned if planned > 0 else ((traveled + remaining) if remaining is not None else 0.0)
    if arrived:
        progress = 1.0
    elif total > 0 and remaining is not None:
        progress = min(1.0, max(0.0, (total - remaining) / total))
    elif total > 0:
        progress = min(1.0, traveled / total)
    else:
        progress = 0.0

    # ETA = distance restante / vitesse effective (réelle si en mouvement franc, sinon réf.).
    ref_speed = float(speed) if (speed is not None and float(speed) >= ETA_REF_MIN_SPEED_KMH) else AVG_SPEED_KMH
    if arrived:
        eta = 0
    elif remaining is not None and remaining > 0:
        eta = max(1, math.ceil(remaining / ref_speed * 60))  # ceil : ne jamais sous-estimer
    else:
        dur = rt.get("duration_min")
        eta = max(1, math.ceil(dur * (1 - progress))) if dur else None

    return {
        "eta_min": eta,
        "remaining_km": remaining,  # None si inconnu (cohérent avec eta_min)
        "traveled_km": round(traveled, 2),
        "progress": round(progress, 3),
    }


# --- Ingestion des positions réelles -------------------------------------


def _get_session(trip):
    session = trip.tracking_sessions.filter(status="active").first()
    if session is None:
        session = TripTrackingSession.objects.create(
            subsidiary=trip.subsidiary, trip=trip, status="active",
            started_at=trip.actual_departure or timezone.now(),
        )
    return session


def real_traveled_km(trip) -> float:
    """Distance réellement parcourue : somme des segments entre points GPS enregistrés.

    Les sauts GPS (vitesse implicite > MAX_PLAUSIBLE_KMH) sont exclus du cumul.
    """
    total = 0.0
    for session in trip.tracking_sessions.all():
        prev = prev_t = None
        for lat, lng, t in session.points.order_by("recorded_at").values_list(
            "latitude", "longitude", "recorded_at"
        ):
            cur = [float(lat), float(lng)]
            if prev is not None:
                d = _haversine(prev, cur)
                dt = (t - prev_t).total_seconds() if prev_t else 0
                implied = d / (dt / 3600.0) if dt > 0 else float("inf")
                if implied <= MAX_PLAUSIBLE_KMH:
                    total += d
            prev, prev_t = cur, t
    return round(total, 2)


def close_tracking_sessions(trip):
    """Clôt les sessions actives en figeant distance réelle et vitesse moyenne."""
    now = timezone.now()
    for session in trip.tracking_sessions.filter(status="active"):
        speeds = [
            float(s) for s in session.points.exclude(speed_kmh=None).values_list("speed_kmh", flat=True)
        ]
        session.status = "ended"
        session.ended_at = now
        session.total_distance_km = Decimal(str(real_traveled_km(trip)))
        if speeds:
            session.average_speed_kmh = Decimal(str(round(sum(speeds) / len(speeds), 2)))
        session.save(update_fields=[
            "status", "ended_at", "total_distance_km", "average_speed_kmh", "updated_at",
        ])


def record_position(
    user, trip_id, latitude, longitude,
    speed_kmh=None, heading=None, accuracy_m=None, recorded_at=None,
) -> dict:
    """Enregistre une position GPS réelle envoyée par l'appareil d'un participant.

    `recorded_at` (datetime, optionnel) : horodatage GPS d'origine pour les points
    mis en tampon hors-ligne et rejoués au retour réseau. Sans lui, l'instant courant
    est utilisé (point « live ») avec anti-spam et dérivation de vitesse.
    Retourne {"ok": bool, "detail": str}.
    """
    trip = (
        Trip.objects.accessible_to(user).filter(pk=trip_id)
        .select_related("vehicle", "subsidiary", "driver").first()
    )
    if trip is None:
        return {"ok": False, "status": 404, "detail": "Course introuvable."}
    if trip.status != "in_progress":
        return {"ok": False, "status": 409, "detail": "La course n'est pas en cours."}

    is_participant = (
        user.id == trip.requester_id
        or (trip.driver and trip.driver.user_id == user.id)
        or user.is_superuser or user.has_company_scope
        or user.role in ("fleet_manager", "subsidiary_admin")
    )
    if not is_participant:
        return {"ok": False, "status": 403, "detail": "Vous ne participez pas à cette course."}

    now = timezone.now()
    buffered = recorded_at is not None  # point rejoué depuis le tampon hors-ligne
    stamp = recorded_at or now
    session = _get_session(trip)
    last = session.points.order_by("-recorded_at").first()

    # Anti-spam : uniquement pour les points live (les points tamponnés sont rejoués tels quels).
    if not buffered and last and (now - last.recorded_at).total_seconds() < MIN_INTERVAL_S:
        return {"ok": True, "detail": "Position ignorée (trop rapprochée)."}

    lat, lng = float(latitude), float(longitude)
    # Vitesse : fournie par le capteur, sinon dérivée de l'intervalle (points live seulement).
    if speed_kmh is None and last and not buffered:
        dt = (now - last.recorded_at).total_seconds()
        if 1 <= dt <= STALE_AFTER_S:
            d = _haversine([float(last.latitude), float(last.longitude)], [lat, lng])
            derived = round(d / (dt / 3600.0), 2)
            # Saut GPS : vitesse implicite impossible → vitesse inconnue.
            speed_kmh = derived if derived <= MAX_PLAUSIBLE_KMH else None

    TripLocationPoint.objects.create(
        session=session,
        latitude=Decimal(str(round(lat, 6))), longitude=Decimal(str(round(lng, 6))),
        speed_kmh=Decimal(str(speed_kmh)) if speed_kmh is not None else None,
        accuracy_m=Decimal(str(accuracy_m)) if accuracy_m is not None else None,
        recorded_at=stamp,
    )
    # Position « dernière connue » : ne recule jamais dans le temps (rejeu tardif).
    loc = VehicleLocation.objects.filter(vehicle=trip.vehicle).first()
    if loc is None or loc.recorded_at is None or stamp >= loc.recorded_at:
        loc, _ = VehicleLocation.objects.update_or_create(
            vehicle=trip.vehicle,
            defaults={
                "latitude": Decimal(str(round(lat, 6))),
                "longitude": Decimal(str(round(lng, 6))),
                "speed_kmh": Decimal(str(speed_kmh)) if speed_kmh is not None else None,
                "heading": Decimal(str(heading)) if heading is not None else None,
                "recorded_at": stamp,
            },
        )
        # Géofencing : alertes d'entrée/sortie sur transition (sur la position la plus récente).
        try:
            from apps.tracking.geofence import check_geofences

            check_geofences(trip.vehicle, loc, trip)
        except Exception:
            pass

    # #12 — détection d'anomalies (détour / arrêt prolongé) sur les points live.
    if not buffered:
        try:
            detect_anomalies(trip, lat, lng, speed_kmh, now)
        except Exception:
            pass
    return {"ok": True, "detail": "Position enregistrée.",
            "speed_kmh": float(speed_kmh) if speed_kmh is not None else None}


# --- Lectures (flotte, course) -------------------------------------------


def _fresh_speed(loc, now=None):
    """Vitesse uniquement si la position est récente — sinon inconnue (None)."""
    if not loc or loc.speed_kmh is None or not loc.recorded_at:
        return None
    now = now or timezone.now()
    if (now - loc.recorded_at).total_seconds() > STALE_AFTER_S:
        return None
    return loc.speed_kmh


def _position_rows(vehicles, now=None) -> list[dict]:
    """Construit les lignes de position pour une liste de véhicules (lecture seule)."""
    now = now or timezone.now()
    vehicles = list(vehicles)
    locations = {l.vehicle_id: l for l in VehicleLocation.objects.filter(vehicle__in=vehicles)}
    active = {
        t.vehicle_id: t
        for t in Trip.objects.filter(vehicle__in=vehicles, status="in_progress")
        .select_related("driver", "reservation", "route")
    }
    rows = []
    for v in vehicles:
        loc = locations.get(v.id)
        trip = active.get(v.id)
        speed = _fresh_speed(loc, now)
        is_late = bool(trip and trip.reservation.estimated_return and trip.reservation.estimated_return < now)
        rows.append({
            "id": str(v.id),
            "registration": v.registration,
            "brand": v.brand,
            "model": v.model,
            "status": v.status,
            "status_display": v.get_status_display(),
            "subsidiary": str(v.subsidiary_id),  # pour le filtrage côté consumer (fan-out)
            "subsidiary_name": v.subsidiary.name,
            "driver_name": trip.driver.full_name if (trip and trip.driver) else None,
            "destination": trip.destination if trip else None,
            "trip_id": str(trip.id) if trip else None,
            "latitude": str(loc.latitude) if loc else None,
            "longitude": str(loc.longitude) if loc else None,
            "speed_kmh": str(speed) if speed is not None else None,
            "heading": str(loc.heading) if (loc and loc.heading is not None) else None,
            "recorded_at": loc.recorded_at.isoformat() if (loc and loc.recorded_at) else None,
            "is_late": is_late,
        })
    return rows


def compute_positions(user, subsidiary_id=None) -> list[dict]:
    """Dernières positions réelles connues des véhicules du périmètre (lecture seule).

    Utilisé par l'API REST (chargement initial / repli). Le temps réel passe par le
    diffuseur (broadcast_tracking) + groupes Redis.
    """
    vehicles = Vehicle.objects.for_user(user).select_related("subsidiary")
    if subsidiary_id and user.has_company_scope:
        vehicles = vehicles.filter(subsidiary_id=subsidiary_id)
    return _position_rows(vehicles)


def compute_all_positions() -> list[dict]:
    """Positions de TOUTE la flotte (sans scoping) — source du diffuseur temps réel.

    La flotte est mutualisée : tous les véhicules sont visibles par tous. Le filtrage
    éventuel par filiale est appliqué côté consumer à partir du champ `subsidiary`.
    """
    return _position_rows(Vehicle.objects.select_related("subsidiary").all())


AVG_SPEED_KMH = 28.0


def trip_tracking(user, trip_id, allow_provision=True) -> dict | None:
    """Instantané de suivi d'une course : position véhicule réelle, état, ETA, itinéraire.

    `user=None` : pas de scoping (réservé au diffuseur ; l'accès est déjà contrôlé à la
    connexion WebSocket avant de rejoindre le groupe).
    """
    base = Trip.objects.all() if user is None else Trip.objects.accessible_to(user)
    trip = (
        base.filter(pk=trip_id)
        .select_related("vehicle", "driver", "reservation", "route").first()
    )
    if trip is None:
        return None

    loc = VehicleLocation.objects.filter(vehicle=trip.vehicle).first()
    speed = _fresh_speed(loc)

    rt = trip_route(user, trip_id, allow_provision=allow_provision) or {}
    # ETA / distances / progression robustes (cf. _live_metrics) : tiennent même sans
    # itinéraire prévu, en s'appuyant sur la position GPS courante et la destination.
    m = _live_metrics(rt, loc, speed)

    return {
        "trip_id": str(trip.id),
        "status": trip.status,
        "status_display": trip.get_status_display(),
        "destination": trip.destination,
        "driver_name": trip.driver.full_name if trip.driver_id else None,
        "vehicle": {
            "registration": trip.vehicle.registration,
            "latitude": str(loc.latitude) if loc else None,
            "longitude": str(loc.longitude) if loc else None,
            "speed_kmh": str(speed) if speed is not None else None,
        },
        "eta_min": m["eta_min"],
        "distance_km": rt.get("distance_km", 0.0),
        "traveled_km": m["traveled_km"],
        "remaining_km": m["remaining_km"],
        "progress": m["progress"],
        "planned": rt.get("planned", []),
        "rerouted": rt.get("rerouted", []),
        "reroute_count": rt.get("reroute_count", 0),
        "actual": rt.get("actual", []),
        "destination_point": rt.get("destination_point"),
    }


def trip_route(user, trip_id, allow_provision=True) -> dict | None:
    """Itinéraire prévu (tracé routier) + trace GPS réelle + distance/progression/vitesse.

    `user=None` : pas de scoping (utilisé par le diffuseur temps réel)."""
    base = Trip.objects.all() if user is None else Trip.objects.accessible_to(user)
    trip = (
        base.filter(pk=trip_id)
        .select_related("route", "vehicle", "driver", "reservation").first()
    )
    if trip is None:
        return None

    # Provisionne l'itinéraire (géocodage origine/destination + OSRM) s'il manque — idempotent.
    route = ensure_trip_route(trip, allow_provision=allow_provision)
    planned = ensure_geometry(route) if route else []
    distance_km = float(route.planned_distance_km) if (route and route.planned_distance_km) else 0.0
    duration_min = route.planned_duration_min if route else None

    dest_point = None
    if route and route.destination_lat is not None:
        dest_point = [float(route.destination_lat), float(route.destination_lng)]
    elif planned:
        dest_point = planned[-1]

    # Trace réelle (150 derniers points par session)
    actual: list[list[float]] = []
    for session in trip.tracking_sessions.all():
        recent = list(session.points.order_by("-recorded_at")[:150])
        recent.reverse()
        actual.extend([float(p.latitude), float(p.longitude)] for p in recent)

    # Progression réelle : km parcourus (GPS) rapportés à l'itinéraire prévu.
    traveled_km = real_traveled_km(trip)
    progress = min(1.0, traveled_km / distance_km) if distance_km else 0.0

    loc = getattr(trip.vehicle, "last_location", None)
    speed = _fresh_speed(loc)

    # #3C — itinéraire recalculé (détour) : tracé actif distinct du prévu d'origine.
    rerouted = list(route.active_geometry) if (route and route.active_geometry) else []
    reroute_count = route.reroute_count if route else 0

    return {
        "trip_id": str(trip.id),
        "destination": trip.destination,
        "destination_point": dest_point,
        "planned": planned,
        "rerouted": rerouted,
        "reroute_count": reroute_count,
        "rerouted_distance_km": float(route.active_distance_km) if (route and route.active_distance_km) else None,
        "rerouted_duration_min": route.active_duration_min if route else None,
        "last_rerouted_at": route.last_rerouted_at.isoformat() if (route and route.last_rerouted_at) else None,
        "actual": actual,
        "distance_km": distance_km,
        "traveled_km": traveled_km,
        "remaining_km": round(max(0.0, distance_km - traveled_km), 2),
        "duration_min": duration_min,
        "progress": round(progress, 3),
        "speed_kmh": float(speed) if speed is not None else None,
        "driver_name": trip.driver.full_name if trip.driver else None,
    }


# Nombre maximal de points renvoyés pour la relecture (sous-échantillonnage au besoin).
REPLAY_MAX_POINTS = 2000


def trip_replay(user, trip_id) -> dict | None:
    """Trace GPS complète et horodatée d'une course, pour la relecture temporelle.

    Renvoie les points ordonnés dans le temps : [lat, lng, t (ISO), speed|None].
    Sous-échantillonné à REPLAY_MAX_POINTS pour les très longs trajets.
    """
    base = Trip.objects.all() if user is None else Trip.objects.accessible_to(user)
    trip = base.filter(pk=trip_id).select_related("route", "vehicle").first()
    if trip is None:
        return None

    raw = []
    for session in trip.tracking_sessions.all():
        for p in session.points.order_by("recorded_at").values_list(
            "latitude", "longitude", "recorded_at", "speed_kmh"
        ):
            raw.append(p)
    raw.sort(key=lambda p: p[2])

    # Sous-échantillonnage régulier si la trace est très longue.
    if len(raw) > REPLAY_MAX_POINTS:
        step = len(raw) / REPLAY_MAX_POINTS
        raw = [raw[int(i * step)] for i in range(REPLAY_MAX_POINTS)]

    points = [
        [float(lat), float(lng), rec.isoformat(), float(spd) if spd is not None else None]
        for (lat, lng, rec, spd) in raw
    ]
    route = getattr(trip, "route", None)
    planned = route.geometry if (route and route.geometry) else []
    return {
        "trip_id": str(trip.id),
        "destination": trip.destination,
        "vehicle_registration": trip.vehicle.registration if trip.vehicle_id else None,
        "planned": planned,
        "points": points,
        "distance_km": real_traveled_km(trip),
        "started_at": points[0][2] if points else None,
        "ended_at": points[-1][2] if points else None,
    }
