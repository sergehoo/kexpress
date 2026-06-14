"""Endpoints de la vue carte : géocodage, estimation d'itinéraire, véhicules proches."""
import json
import urllib.parse
import urllib.request

from django.conf import settings
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.analytics.scope import scoped
from apps.tracking.live import _haversine
from apps.tracking.osrm import _ctx, road_route

COST_PER_KM = float(getattr(settings, "MAP_COST_PER_KM", 350))
AVG_SPEED_KMH = 28.0


#: Pays prioritaire pour les suggestions d'adresses (codes ISO, séparés par des virgules).
PLACES_PRIORITY_COUNTRIES = getattr(settings, "PLACES_PRIORITY_COUNTRIES", "ci")

#: Base Nominatim (publique par défaut, auto-hébergeable via NOMINATIM_URL).
NOMINATIM_URL = getattr(settings, "NOMINATIM_URL", "https://nominatim.openstreetmap.org").rstrip("/")


class PlacesSearchView(APIView):
    """Autocomplétion de lieux via OpenStreetMap Nominatim (+ filiales internes).

    Les résultats de la zone prioritaire (Côte d'Ivoire par défaut) arrivent en tête ;
    une recherche mondiale complète la liste si nécessaire.
    """

    permission_classes = [IsAuthenticated]

    def _nominatim(self, q: str, limit: int, countrycodes: str | None = None) -> list[dict]:
        params = {
            "q": q, "format": "json", "limit": limit,
            "addressdetails": 0, "accept-language": "fr",
        }
        if countrycodes:
            params["countrycodes"] = countrycodes
        url = f"{NOMINATIM_URL}/search?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "KaydanExpress/1.0 (fleet)"})
        try:
            with urllib.request.urlopen(req, timeout=8, context=_ctx()) as resp:
                data = json.load(resp)
            return [
                {
                    "label": r.get("display_name", ""),
                    "lat": float(r["lat"]),
                    "lng": float(r["lon"]),
                    "internal": False,
                }
                for r in data
            ]
        except Exception:
            return []

    def get(self, request):
        q = (request.query_params.get("q") or "").strip()
        if len(q) < 3:
            return Response({"results": []})

        results = []
        # Lieux internes : filiales correspondant à la requête.
        from apps.organizations.models import Subsidiary

        for s in Subsidiary.objects.filter(name__icontains=q)[:3]:
            results.append({"label": f"🏢 {s.name} ({s.city})", "lat": None, "lng": None, "internal": True})

        # 1) Zone prioritaire (Côte d'Ivoire), puis 2) complément mondial (dédupliqué).
        priority = self._nominatim(q, limit=6, countrycodes=PLACES_PRIORITY_COUNTRIES)
        results.extend(priority)
        if len(priority) < 4:
            seen = {r["label"] for r in results}
            for r in self._nominatim(q, limit=4):
                if r["label"] not in seen:
                    results.append(r)
        return Response({"results": results[:10]})


class PlacesReverseView(APIView):
    """Géocodage inverse Nominatim : coordonnées GPS → adresse lisible."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            lat = float(request.query_params.get("lat"))
            lng = float(request.query_params.get("lng"))
        except (TypeError, ValueError):
            return Response({"detail": "Paramètres lat/lng requis."}, status=400)

        params = urllib.parse.urlencode({
            "lat": lat, "lon": lng, "format": "json",
            "accept-language": "fr", "zoom": 17,
        })
        url = f"{NOMINATIM_URL}/reverse?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "KaydanExpress/1.0 (fleet)"})
        label = None
        try:
            with urllib.request.urlopen(req, timeout=8, context=_ctx()) as resp:
                data = json.load(resp)
            label = data.get("display_name")
        except Exception:
            pass
        return Response({"label": label, "lat": lat, "lng": lng})


class RouteEstimateView(APIView):
    """Estime distance, durée et consommation carburant pour un trajet.

    L'employé ne voit que l'information opérationnelle (distance / durée / litres /
    niveau d'impact). Le coût carburant n'est exposé qu'aux gestionnaires.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        from django.utils import timezone

        from apps.fuelintel.access import can_see_costs
        from apps.fuelintel.engine import estimate_fuel, fuel_cost

        origin = request.data.get("origin")
        destination = request.data.get("destination")
        if not (origin and destination and len(origin) == 2 and len(destination) == 2):
            return Response({"detail": "Origine et destination (lat,lng) requises."}, status=400)

        route = road_route([(float(origin[0]), float(origin[1])), (float(destination[0]), float(destination[1]))])
        distance = route["distance_km"] or round(_haversine([float(origin[0]), float(origin[1])], [float(destination[0]), float(destination[1])]), 2)
        duration = route["duration_min"] or round(distance / AVG_SPEED_KMH * 60, 1)
        available = scoped(request.user)["vehicles"].filter(status="available").count()

        est = estimate_fuel(
            distance,
            subsidiary_id=getattr(request.user, "subsidiary_id", None),
            departure_time=timezone.now(),
        )
        payload = {
            "geometry": route["geometry"],
            "distance_km": distance,
            "duration_min": duration,
            "fuel_liters": float(est["liters"]),
            "energy_level": est["level"],
            "available_vehicles": available,
        }
        # Coût carburant : gestionnaires uniquement (jamais l'employé demandeur).
        if can_see_costs(request.user):
            cost = fuel_cost(est["liters"], "diesel")
            if cost:
                payload["fuel_cost"] = float(cost["cost"])
                payload["fuel_price"] = float(cost["price"])
                payload["fuel_price_date"] = cost["price_date"].isoformat() if cost["price_date"] else None
        return Response(payload)


class RouteCalculateView(APIView):
    """Itinéraire détaillé via OSRM : géométrie + distance + durée + guidage turn-by-turn.

    Corps : {origin:[lat,lng], destination:[lat,lng], waypoints?:[[lat,lng],...]}.
    Sert la navigation chauffeur (#4) et le recalcul d'itinéraire.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        origin = request.data.get("origin")
        destination = request.data.get("destination")
        waypoints = request.data.get("waypoints") or []
        if not (origin and destination and len(origin) == 2 and len(destination) == 2):
            return Response({"detail": "Origine et destination (lat,lng) requises."}, status=400)
        try:
            pts = (
                [(float(origin[0]), float(origin[1]))]
                + [(float(w[0]), float(w[1])) for w in waypoints]
                + [(float(destination[0]), float(destination[1]))]
            )
        except (TypeError, ValueError):
            return Response({"detail": "Coordonnées invalides."}, status=400)

        route = road_route(pts, steps=True)
        distance = route["distance_km"] or round(_haversine(pts[0], pts[-1]), 2)
        duration = route["duration_min"] or round(distance / AVG_SPEED_KMH * 60, 1)
        return Response({
            "geometry": route["geometry"],
            "distance_km": distance,
            "duration_min": duration,
            "eta_min": duration,
            "steps": route.get("steps", []),
        })


class NearbyVehiclesView(APIView):
    """Véhicules disponibles les plus proches d'un point, triés par distance + ETA."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            lat = float(request.query_params.get("lat"))
            lng = float(request.query_params.get("lng"))
        except (TypeError, ValueError):
            return Response({"detail": "Paramètres lat/lng requis."}, status=400)

        from apps.maps.proximity import rank_by_eta

        vehicles = (
            scoped(request.user)["vehicles"]
            .filter(status="available", last_location__isnull=False)
            .select_related("subsidiary", "last_location")
        )
        candidates = [{
            "id": str(v.id),
            "registration": v.registration,
            "brand": v.brand,
            "model": v.model,
            "vehicle_type_display": v.get_vehicle_type_display(),
            "capacity": v.capacity,
            "subsidiary_name": v.subsidiary.name,
            "lat": float(v.last_location.latitude),
            "lng": float(v.last_location.longitude),
        } for v in vehicles]
        # Classement par ETA routier OSRM (repli haversine si OSRM indisponible).
        rows = [
            {**c, "latitude": str(c.pop("lat")), "longitude": str(c.pop("lng"))}
            for c in rank_by_eta((lat, lng), candidates)
        ]

        # Suggestion K-BOT ancrée sur les données : meilleur véhicule + disponibilité.
        if rows:
            best = rows[0]
            level = "bonne disponibilité" if len(rows) >= 3 else "disponibilité limitée"
            suggestion = (
                f"K-BOT recommande {best['registration']} ({best['brand']} {best['model']}, "
                f"{best['capacity']} pl.) — le plus proche à {best['distance_km']} km, "
                f"arrivée estimée ~{best['eta_min']} min. {len(rows)} véhicule(s) autour de vous : {level}."
            )
        elif vehicles.exists():
            suggestion = (
                "Des véhicules sont disponibles mais sans position GPS récente. "
                "K-BOT suggère de contacter le gestionnaire de flotte pour une affectation manuelle."
            )
        else:
            suggestion = (
                "Aucun véhicule disponible actuellement sur votre périmètre. "
                "K-BOT suggère de planifier la course pour plus tard ou d'élargir la recherche."
            )

        return Response({"count": len(rows), "results": rows[:10], "suggestion": suggestion})


class DriversNearestView(APIView):
    """Chauffeurs disponibles les plus proches d'un point (ETA OSRM).

    La position d'un chauffeur est déduite du véhicule de sa dernière course
    (faute de GPS chauffeur dédié) : les chauffeurs localisables sont classés par
    ETA, les autres listés ensuite.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            lat = float(request.query_params.get("lat"))
            lng = float(request.query_params.get("lng"))
        except (TypeError, ValueError):
            return Response({"detail": "Paramètres lat/lng requis."}, status=400)

        from apps.drivers.models import Driver
        from apps.maps.proximity import driver_last_location, rank_by_eta

        drivers = (
            Driver.objects.for_user(request.user)
            .filter(is_available=True)
            .select_related("subsidiary")
        )
        candidates, unlocated = [], []
        for d in drivers:
            loc = driver_last_location(d)
            base = {
                "id": str(d.id), "full_name": d.full_name, "matricule": d.matricule,
                "subsidiary_name": d.subsidiary.name,
            }
            if loc:
                candidates.append({**base, "lat": loc[0], "lng": loc[1]})
            else:
                unlocated.append(base)

        ranked = [
            {**c, "latitude": str(c.pop("lat")), "longitude": str(c.pop("lng"))}
            for c in rank_by_eta((lat, lng), candidates)
        ]
        return Response({
            "count": len(ranked) + len(unlocated),
            "results": ranked[:10],
            "unlocated": unlocated[:10],
        })
