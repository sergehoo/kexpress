"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Calendar,
  Car,
  CheckCircle2,
  ChevronDown,
  Clock,
  CornerUpLeft,
  CornerUpRight,
  ExternalLink,
  Flag,
  Gauge,
  LocateFixed,
  MapPin,
  Navigation,
  Route,
  RotateCw,
  Sparkles,
  Users,
  Wallet,
} from "lucide-react";

import { Button, Input, Select, Spinner } from "@/components/ui";
import { PlaceSearch, type Point } from "@/components/PlaceSearch";
import { api, apiError } from "@/lib/api";
import { useFleetLive } from "@/lib/useFleetLive";
import { useGpsTracker } from "@/lib/useGpsTracker";
import { useTripTracking } from "@/lib/useTripTracking";
import { useActiveTrip, useDriverMissions, useNearbyVehicles, useTripRoute } from "@/lib/queries";
import { useAuth } from "@/lib/auth";
import { currentMission, googleMapsUrl, wazeUrl } from "@/lib/driver";
import { DriverMissionPanel, DriverNoMissionPanel } from "@/components/DriverMissionPanel";
import type { RouteCalculation, RouteStep, RouteEstimate, VehiclePosition } from "@/lib/types";
import { cn } from "@/lib/utils";

const MapView = dynamic(() => import("@/components/MapView"), {
  ssr: false,
  loading: () => (
    <div className="flex h-full items-center justify-center bg-surface2"><Spinner className="h-7 w-7" /></div>
  ),
});


export default function MapPage() {

  const { me } = useAuth();
  const isDriver = me?.role === "driver";

  // Suivi temps réel : si l'utilisateur a une course en cours, on bascule en mode suivi.
  const { data: activeTrip } = useActiveTrip();
  const { data: track, connected: trackConnected } = useTripTracking(activeTrip?.id);
  const trackingMode = Boolean(activeTrip && track);
  // Le chauffeur ne consomme pas les positions de flotte : on n'ouvre pas le WS flotte pour lui.
  const { positions: fleetPositions } = useFleetLive(undefined, !trackingMode && !isDriver);
  // GPS réel : l'appareil du demandeur alimente le tracking pendant la course.
  const gps = useGpsTracker(activeTrip?.id, activeTrip?.status === "in_progress");

  // --- Espace chauffeur (mission-first) ---
  const qc = useQueryClient();
  const { data: driverMissions, isLoading: missionsLoading } = useDriverMissions(isDriver);
  const dvMission = isDriver ? currentMission(driverMissions) : null;
  const dvScheduled = dvMission && dvMission.status === "scheduled" ? dvMission : null;
  // Itinéraire prévu de la mission planifiée (pour tracé carte + estimations).
  const { data: dvRoute } = useTripRoute(!trackingMode && dvScheduled ? dvScheduled.trip_id : undefined);
  const onMissionStarted = () => {
    qc.invalidateQueries({ queryKey: ["active-trip"] });
    qc.invalidateQueries({ queryKey: ["driver-missions"] });
  };

  // La carte temps réel reste TOUJOURS accessible au chauffeur. Selon son état, le panneau
  // adapté s'affiche en surimpression (mission planifiée / course en cours / aucune mission) —
  // le chauffeur ne voit jamais le formulaire de réservation (réservé aux demandeurs).
  // L'espace dédié reste accessible via la barre latérale (« Mes missions ») et les liens des panneaux.

  // Véhicule suivi représenté comme une position de flotte (pour MapView).
  const trackedPositions: VehiclePosition[] = track && track.vehicle.latitude
    ? [{
        id: track.trip_id, registration: track.vehicle.registration, brand: "", model: "",
        status: "on_trip", status_display: track.status_display, subsidiary_name: "",
        driver_name: track.driver_name, destination: track.destination, trip_id: track.trip_id,
        latitude: track.vehicle.latitude, longitude: track.vehicle.longitude,
        speed_kmh: track.vehicle.speed_kmh, heading: null,
        recorded_at: new Date().toISOString(), is_late: false,
      }]
    : [];
  const positions = trackingMode ? trackedPositions : fleetPositions;

  const [origin, setOrigin] = useState<Point | null>(null);
  const [destination, setDestination] = useState<Point | null>(null);
  const [recenter, setRecenter] = useState<[number, number] | null>(null);
  const [estimate, setEstimate] = useState<RouteEstimate | null>(null);
  const [estimating, setEstimating] = useState(false);
  const [reserving, setReserving] = useState(false);
  const [reserved, setReserved] = useState<{ id: string; status: string } | null>(null);
  const [locating, setLocating] = useState(false);
  const [error, setError] = useState("");
  // Panneau de réservation repliable (mobile) pour libérer la vue de la carte.
  const [collapsed, setCollapsed] = useState(false);

  const today = new Date().toISOString().slice(0, 10);
  const [form, setForm] = useState({
    date: today, time: "08:00", purpose: "", passengers: 1,
    needs_driver: true, priority: "normal",
    trip_type: "one_way", return_date: today, return_time: "17:00",
  });
  const set = (k: string, v: unknown) => setForm((f) => ({ ...f, [k]: v }));
  const isRoundTrip = form.trip_type === "round_trip";

  const nearby = useNearbyVehicles(origin?.lat, origin?.lng);

  // Estimation automatique dès que départ + destination sont définis.
  useEffect(() => {
    if (!origin || !destination) { setEstimate(null); return; }
    setEstimating(true);
    api.post<RouteEstimate>("/routes/estimate/", {
      origin: [origin.lat, origin.lng], destination: [destination.lat, destination.lng],
    }).then(({ data }) => setEstimate(data)).catch(() => setEstimate(null)).finally(() => setEstimating(false));
  }, [origin, destination]);

  function useMyPosition() {
    setError("");
    // La géolocalisation exige un contexte sécurisé (HTTPS) — fréquent sur mobile en HTTP.
    if (typeof window !== "undefined" && !window.isSecureContext) {
      setError("La géolocalisation nécessite une connexion sécurisée (HTTPS).");
      return;
    }
    if (typeof navigator === "undefined" || !navigator.geolocation) {
      setError("Géolocalisation indisponible sur cet appareil.");
      return;
    }
    setLocating(true);

    const onOk = async (pos: GeolocationPosition) => {
      const lat = pos.coords.latitude, lng = pos.coords.longitude;
      // Géocodage inverse : remplit le champ départ avec l'adresse réelle.
      let label = "Ma position actuelle";
      try {
        const { data } = await api.get<{ label: string | null }>("/places/reverse/", { params: { lat, lng } });
        if (data.label) label = data.label;
      } catch { /* repli : libellé générique */ }
      setOrigin({ lat, lng, label });
      setRecenter([lat, lng]);
      setLocating(false);
    };

    const onErr = (err: GeolocationPositionError, isRetry: boolean) => {
      // Sur mobile, la haute précision (GPS) expire souvent : 2e essai en précision
      // réduite (Wi-Fi/réseau), plus tolérant, avant d'abandonner.
      if (err.code === err.TIMEOUT && !isRetry) {
        navigator.geolocation.getCurrentPosition(
          onOk,
          (e) => onErr(e, true),
          { enableHighAccuracy: false, timeout: 20_000, maximumAge: 120_000 },
        );
        return;
      }
      setLocating(false);
      if (err.code === err.PERMISSION_DENIED) {
        setError("Accès à la position refusé. Autorisez la localisation pour ce site (icône cadenas → Autorisations) puis réessayez, ou choisissez le départ manuellement.");
      } else if (err.code === err.POSITION_UNAVAILABLE) {
        setError("Position introuvable (signal GPS faible). Réessayez à l'extérieur, ou choisissez le départ manuellement.");
      } else if (err.code === err.TIMEOUT) {
        setError("La localisation a expiré. Réessayez, ou choisissez le départ manuellement.");
      } else {
        setError("Localisation impossible. Choisissez un point de départ manuellement.");
      }
    };

    navigator.geolocation.getCurrentPosition(
      onOk,
      (e) => onErr(e, false),
      { enableHighAccuracy: true, timeout: 10_000, maximumAge: 30_000 },
    );
  }

  async function onMapClick(lat: number, lng: number) {
    let label = "Point sélectionné sur la carte";
    try {
      const { data } = await api.get<{ label: string | null }>("/places/reverse/", { params: { lat, lng } });
      if (data.label) label = data.label;
    } catch { /* repli : libellé générique */ }
    setDestination({ lat, lng, label });
  }

  async function reserve(submit: boolean) {
    setError("");
    if (!origin || !destination) { setError("Définissez un point de départ et une destination."); return; }
    if (!form.purpose) { setError("Précisez le motif de la course."); return; }
    const dep = new Date(`${form.date}T${form.time}:00`);
    const legMin = (estimate ? estimate.duration_min + 30 : 60);
    // Aller-retour : départ du retour précisé par l'usager ; la fenêtre se termine après
    // le trajet retour. La destination du retour est le point de départ (géré côté serveur).
    let returnAt: Date | null = null;
    let endWindow = new Date(dep.getTime() + legMin * 60000);
    if (isRoundTrip) {
      returnAt = new Date(`${form.return_date}T${form.return_time}:00`);
      if (returnAt <= dep) { setError("L'heure de retour doit être après le départ."); return; }
      endWindow = new Date(returnAt.getTime() + legMin * 60000);
    }
    setReserving(true);
    try {
      const { data } = await api.post("/reservations/from-map/", {
        origin: origin.label, destination: destination.label,
        departure_time: dep.toISOString(), estimated_return: endWindow.toISOString(),
        trip_type: form.trip_type,
        return_time: returnAt ? returnAt.toISOString() : undefined,
        purpose: form.purpose, passengers: form.passengers, needs_driver: form.needs_driver,
        priority: form.priority, submit,
      });
      setReserved({ id: data.id, status: data.status_display });
    } catch (e) { setError(apiError(e)); } finally { setReserving(false); }
  }

  function reset() {
    setReserved(null); setOrigin(null); setDestination(null); setEstimate(null);
    setForm((f) => ({ ...f, purpose: "" }));
  }

  return (
    <div className="relative isolate h-[calc(100vh-8rem)] min-h-[28rem] overflow-hidden rounded-[var(--radius-card)] border border-line">
      <MapView
        positions={positions}
        origin={trackingMode ? null : isDriver ? (dvRoute?.planned?.[0] ?? null) : origin ? [origin.lat, origin.lng] : null}
        destination={
          trackingMode
            ? track?.destination_point ?? null
            : isDriver
              ? (dvRoute?.destination_point ?? dvScheduled?.route?.destination_point ?? null)
              : destination ? [destination.lat, destination.lng] : null
        }
        planned={trackingMode ? (track?.rerouted?.length ? track.rerouted : track?.planned) : isDriver ? dvRoute?.planned : estimate?.geometry}
        actual={trackingMode ? track?.actual : isDriver ? dvRoute?.actual : undefined}
        recenterTo={
          trackingMode && track?.vehicle.latitude
            ? [Number(track.vehicle.latitude), Number(track.vehicle.longitude)]
            : isDriver
              ? (dvRoute?.destination_point ?? null)
              : recenter
        }
        onMapClick={trackingMode || reserved || isDriver ? undefined : onMapClick}
      />

      {trackingMode && track ? (
        <TrackingPanel track={track} connected={trackConnected} gpsActive={gps.active} gpsError={gps.error} />
      ) : isDriver ? (
        dvScheduled ? (
          <DriverMissionPanel mission={dvScheduled} route={dvRoute} onStarted={onMissionStarted} />
        ) : missionsLoading ? null : (
          <DriverNoMissionPanel />
        )
      ) : (
        <div className="absolute inset-x-2 bottom-2 z-[500] rounded-2xl border border-line bg-surface/95 shadow-2xl backdrop-blur lg:inset-x-auto lg:bottom-auto lg:right-4 lg:top-4 lg:w-[24rem]">
          {/* En-tête : réductible en mobile pour libérer la carte ; toujours ouvert en desktop. */}
          <div className="flex items-center gap-2 px-4 py-3">
            <Navigation className="h-5 w-5 shrink-0 text-brand-500" />
            <h2 className="text-base font-semibold text-ink">{reserved ? "Demande enregistrée" : "Réserver une course"}</h2>
            {collapsed && origin && destination && (
              <span className="hidden min-w-0 flex-1 truncate text-xs text-muted sm:inline">→ {destination.label}</span>
            )}
            <button
              type="button"
              onClick={() => setCollapsed((c) => !c)}
              aria-label={collapsed ? "Agrandir le panneau" : "Réduire le panneau"}
              aria-expanded={!collapsed}
              className="ml-auto shrink-0 rounded-md p-1 text-muted hover:bg-surface2 lg:hidden"
            >
              <ChevronDown className={cn("h-5 w-5 transition-transform", !collapsed && "rotate-180")} />
            </button>
          </div>
          <div className={cn("max-h-[52vh] overflow-y-auto px-4 pb-4 lg:max-h-[calc(100vh-13rem)]", collapsed && "hidden lg:block")}>
        {!reserved ? (
          <>
            {/* Départ */}
            <label className="mb-1 block text-xs font-medium text-muted">Point de départ</label>
            <div className="space-y-1.5">
              <PlaceSearch
                placeholder="Rechercher un départ…"
                externalValue={origin?.label}
                onSelect={(p) => { setOrigin(p); setRecenter([p.lat, p.lng]); }}
              />
              <button onClick={useMyPosition} disabled={locating} className="flex items-center gap-1.5 text-xs font-medium text-brand-600 hover:underline disabled:opacity-60">
                <LocateFixed className="h-3.5 w-3.5" /> {locating ? "Localisation en cours…" : "Utiliser ma position actuelle"}
              </button>
              {origin && <p className="truncate text-[11px] text-emerald-600">📍 {origin.label}</p>}
            </div>

            {/* Destination */}
            <label className="mb-1 mt-3 block text-xs font-medium text-muted">Destination</label>
            <PlaceSearch
              placeholder="Rechercher une destination…"
              externalValue={destination?.label}
              onSelect={(p) => { setDestination(p); setRecenter([p.lat, p.lng]); }}
            />
            {destination && <p className="mt-1 truncate text-[11px] text-brand-600">🏁 {destination.label}</p>}
            <p className="mt-1 text-[11px] text-faint">Astuce : cliquez sur la carte pour choisir la destination.</p>

            {/* Estimation */}
            {estimating && <div className="mt-3 flex items-center gap-2 text-xs text-muted"><Spinner className="h-4 w-4" /> Estimation du trajet…</div>}
            {estimate && (
              <div className="mt-3 grid grid-cols-3 gap-2 rounded-xl bg-surface2 p-3 text-center">
                <div><p className="text-sm font-bold text-ink">{estimate.distance_km} km</p><p className="text-[10px] text-faint">distance</p></div>
                <div><p className="text-sm font-bold text-ink">~{Math.round(estimate.duration_min)} min</p><p className="text-[10px] text-faint">durée</p></div>
                <div><p className="text-sm font-bold text-emerald-600">{estimate.fuel_liters.toLocaleString("fr-FR")} L</p><p className="text-[10px] text-faint">conso. estimée</p></div>
              </div>
            )}

            {/* Suggestion K-BOT (ancrée sur les véhicules autour du point de départ) */}
            {origin && nearby.data?.suggestion && (
              <div className="mt-3 rounded-xl border border-brand-500/20 bg-gradient-to-br from-brand-500/10 to-transparent p-3">
                <p className="mb-1 flex items-center gap-1.5 text-xs font-semibold text-brand-600">
                  <Sparkles className="h-3.5 w-3.5" /> Suggestion K-BOT
                </p>
                <p className="text-xs leading-relaxed text-muted">{nearby.data.suggestion}</p>
              </div>
            )}

            {/* Véhicules proches */}
            {origin && (nearby.data?.results.length ?? 0) > 0 && (
              <div className="mt-3">
                <p className="mb-1 text-xs font-medium text-muted">{nearby.data!.results.length} véhicule(s) disponible(s) à proximité</p>
                <div className="space-y-1">
                  {nearby.data!.results.slice(0, 3).map((v) => (
                    <div key={v.id} className="flex items-center gap-2 rounded-lg border border-line px-2.5 py-1.5 text-xs">
                      <Car className="h-3.5 w-3.5 text-sky-600" />
                      <span className="font-medium text-ink">{v.registration}</span>
                      <span className="text-faint">{v.vehicle_type_display}</span>
                      <span className="ml-auto flex items-center gap-1 text-muted"><Clock className="h-3 w-3" />{v.eta_min} min · {v.distance_km} km</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Type de trajet : aller simple / aller-retour */}
            <div className="mt-3 grid grid-cols-2 gap-1 rounded-lg bg-surface2 p-1">
              {([["one_way", "Aller simple"], ["round_trip", "Aller-retour"]] as const).map(([val, label]) => (
                <button
                  key={val}
                  type="button"
                  onClick={() => set("trip_type", val)}
                  className={cn(
                    "rounded-md px-2 py-1.5 text-xs font-medium transition-colors",
                    form.trip_type === val ? "bg-brand-600 text-white shadow-sm" : "text-muted hover:text-ink",
                  )}
                >
                  {label}
                </button>
              ))}
            </div>

            {/* Détails course — aller */}
            <div className="mt-3 grid grid-cols-2 gap-2">
              <div><label className="mb-1 block text-[11px] text-muted"><Calendar className="mr-1 inline h-3 w-3" />{isRoundTrip ? "Départ (aller)" : "Date"}</label><Input type="date" value={form.date} onChange={(e) => set("date", e.target.value)} /></div>
              <div><label className="mb-1 block text-[11px] text-muted"><Clock className="mr-1 inline h-3 w-3" />Heure</label><Input type="time" value={form.time} onChange={(e) => set("time", e.target.value)} /></div>
            </div>

            {/* Retour (aller-retour uniquement) : destination = point de départ */}
            {isRoundTrip && (
              <div className="mt-2 rounded-xl border border-brand-500/20 bg-brand-500/5 p-2.5">
                <p className="mb-1.5 flex items-center gap-1.5 text-[11px] font-medium text-brand-600">
                  <CornerUpLeft className="h-3.5 w-3.5" /> Retour vers {origin?.label ? `« ${origin.label} »` : "le point de départ"}
                </p>
                <div className="grid grid-cols-2 gap-2">
                  <div><label className="mb-1 block text-[11px] text-muted">Date retour</label><Input type="date" value={form.return_date} min={form.date} onChange={(e) => set("return_date", e.target.value)} /></div>
                  <div><label className="mb-1 block text-[11px] text-muted">Heure retour</label><Input type="time" value={form.return_time} onChange={(e) => set("return_time", e.target.value)} /></div>
                </div>
                <p className="mt-1.5 text-[10px] text-faint">Comptabilisé comme 2 voyages (aller + retour).</p>
              </div>
            )}

            <div className="mt-2"><Input placeholder="Motif de la course *" value={form.purpose} onChange={(e) => set("purpose", e.target.value)} /></div>
            <div className="mt-2 grid grid-cols-2 gap-2">
              <div className="relative"><Users className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-faint" /><Input type="number" min={1} value={form.passengers} onChange={(e) => set("passengers", Number(e.target.value))} className="pl-9" /></div>
              <Select value={form.priority} onChange={(e) => set("priority", e.target.value)}>
                <option value="low">Basse</option><option value="normal">Normale</option><option value="high">Haute</option><option value="urgent">Urgente</option>
              </Select>
            </div>
            <label className="mt-2 flex items-center gap-2 text-sm text-muted">
              <input type="checkbox" checked={form.needs_driver} onChange={(e) => set("needs_driver", e.target.checked)} /> Besoin d&apos;un chauffeur
            </label>

            {error && <p className="mt-2 rounded-lg bg-rose-500/10 px-3 py-2 text-xs text-rose-600">{error}</p>}

            <div className="mt-3 flex gap-2">
              <Button className="flex-1" disabled={reserving} onClick={() => reserve(true)}>
                {reserving ? <Spinner className="h-4 w-4 border-white/50 border-t-white" /> : "Réserver maintenant"}
              </Button>
              <Button variant="secondary" disabled={reserving} onClick={() => reserve(false)}>Planifier</Button>
            </div>
          </>
        ) : (
          <div className="space-y-3 text-center">
            <span className="mx-auto flex h-14 w-14 items-center justify-center rounded-full bg-emerald-500/10 text-emerald-600"><CheckCircle2 className="h-7 w-7" /></span>
            <p className="text-sm text-muted">Statut : <span className="font-medium text-ink">{reserved.status}</span></p>
            <div className="rounded-xl bg-surface2 p-3 text-left text-xs text-muted">
              <p>📍 {origin?.label}</p>
              <p className="mt-1">🏁 {destination?.label}</p>
              {estimate && <p className="mt-1">{estimate.distance_km} km · ~{Math.round(estimate.duration_min)} min · {estimate.fuel_liters.toLocaleString("fr-FR")} L</p>}
            </div>
            <div className="flex gap-2">
              <Link href="/reservations" className="flex-1 rounded-lg bg-brand-600 px-3 py-2 text-center text-sm font-medium text-white hover:bg-brand-700">Suivre ma demande</Link>
              <Button variant="secondary" onClick={reset}>Nouvelle course</Button>
            </div>
          </div>
        )}
          </div>
        </div>
      )}
    </div>
  );
}

function TrackingPanel({
  track,
  connected,
  gpsActive,
  gpsError,
}: {
  track: import("@/lib/types").TripTracking;
  connected: boolean;
  gpsActive?: boolean;
  gpsError?: string;
}) {
  const STEPS = [
    { key: "scheduled", label: "Chauffeur affecté" },
    { key: "departed", label: "Chauffeur en route" },
    { key: "in_progress", label: "Course en cours" },
    { key: "returned", label: "Arrivée" },
  ];
  const order: Record<string, number> = { scheduled: 0, departed: 1, in_progress: 2, returned: 3, closed: 3 };
  const cur = order[track.status] ?? 2;

  const qc = useQueryClient();
  const [tcollapsed, setTcollapsed] = useState(false);
  const [endKm, setEndKm] = useState("");
  const [actionBusy, setActionBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  // Déclaration d'incident en course
  const [incidentOpen, setIncidentOpen] = useState(false);
  const [incidentText, setIncidentText] = useState("");
  const [incidentSeverity, setIncidentSeverity] = useState("minor");
  const [incidentBusy, setIncidentBusy] = useState(false);
  const [incidentMsg, setIncidentMsg] = useState("");
  const dest = track.destination_point;
  const inRoute = track.status === "in_progress" || track.status === "departed";

  async function sendIncident() {
    if (!incidentText.trim() || incidentBusy) return;
    setIncidentBusy(true);
    setIncidentMsg("");
    try {
      await api.post(`/trips/${track.trip_id}/report-incident/`, {
        description: incidentText.trim(), severity: incidentSeverity,
      });
      setIncidentText(""); setIncidentOpen(false); setIncidentMsg("Incident signalé au gestionnaire.");
      qc.invalidateQueries({ queryKey: ["incidents"] });
    } catch (e) {
      setIncidentMsg(apiError(e));
    } finally {
      setIncidentBusy(false);
    }
  }

  async function runAction(action: "end" | "close") {
    setActionBusy(true);
    setActionError("");
    try {
      const body = action === "end" && endKm ? { end_mileage: Number(endKm) } : {};
      await api.post(`/trips/${track.trip_id}/${action}/`, body);
      // Rafraîchit la course active : après clôture, /map repasse en mode réservation.
      qc.invalidateQueries({ queryKey: ["active-trip"] });
      qc.invalidateQueries({ queryKey: ["trips"] });
      qc.invalidateQueries({ queryKey: ["fleet-positions"] });
    } catch (e) {
      setActionError(apiError(e));
    } finally {
      setActionBusy(false);
    }
  }

  return (
    <div className="absolute inset-x-2 bottom-2 z-[500] rounded-2xl border border-line bg-surface/95 shadow-2xl backdrop-blur lg:inset-x-auto lg:bottom-auto lg:right-4 lg:top-4 lg:w-[24rem]">
      <div className="flex items-center gap-2 px-4 py-3">
        <Navigation className="h-5 w-5 shrink-0 text-brand-500" />
        <h2 className="text-base font-semibold text-ink">Course en cours</h2>
        <span className="ml-auto flex items-center gap-1.5 text-xs font-medium text-muted">
          <span className={cn("h-2 w-2 rounded-full", connected ? "animate-pulse bg-emerald-500" : "bg-amber-500")} />
          {connected ? "En direct" : "…"}
        </span>
        <button
          type="button"
          onClick={() => setTcollapsed((c) => !c)}
          aria-label={tcollapsed ? "Agrandir le panneau" : "Réduire le panneau"}
          aria-expanded={!tcollapsed}
          className="shrink-0 rounded-md p-1 text-muted hover:bg-surface2 lg:hidden"
        >
          <ChevronDown className={cn("h-5 w-5 transition-transform", !tcollapsed && "rotate-180")} />
        </button>
      </div>

      <div className={cn("max-h-[58vh] overflow-y-auto px-4 pb-4 lg:max-h-[calc(100vh-13rem)]", tcollapsed && "hidden lg:block")}>
      {/* État de l'émetteur GPS de l'appareil (source des positions réelles) */}
      <p className={cn("mb-2 flex items-center gap-1.5 text-[11px]", gpsActive ? "text-emerald-600" : "text-faint")}>
        <LocateFixed className="h-3 w-3" />
        {gpsActive
          ? "GPS de l'appareil actif — positions réelles transmises"
          : gpsError
            ? `GPS indisponible : ${gpsError}`
            : "En attente du GPS de l'appareil…"}
      </p>

      {/* ETA + progression */}
      <div className="rounded-xl bg-gradient-to-br from-brand-500/10 to-transparent p-4">
        <div className="text-center">
          <p className="text-4xl font-bold leading-none text-ink">
            {track.eta_min != null ? track.eta_min : "—"}
            <span className="ml-1 text-base font-medium text-muted">min</span>
          </p>
          <p className="mt-1 text-xs text-muted">
            {track.eta_min != null ? (
              <>arrivée estimée à <span className="font-medium text-ink">{track.destination}</span></>
            ) : (
              "estimation de l'arrivée en cours…"
            )}
          </p>
        </div>
        <div className="mt-3">
          <div className="h-2.5 w-full overflow-hidden rounded-full bg-surface2">
            <div className="h-full rounded-full bg-brand-500 transition-all duration-700" style={{ width: `${Math.round(track.progress * 100)}%` }} />
          </div>
          <p className="mt-1 text-right text-[11px] font-semibold text-brand-600">{Math.round(track.progress * 100)} % du trajet</p>
        </div>
      </div>

      {/* Mesures clés : distance parcourue / restante / vitesse du conducteur */}
      <div className="mt-3 grid grid-cols-3 gap-2">
        <Metric icon={Route} tone="text-emerald-600" value={fmtKm(track.traveled_km)} unit="km" label="Parcouru" />
        <Metric icon={Flag} tone="text-brand-600" value={fmtKm(track.remaining_km)} unit="km" label="Restant" />
        <Metric icon={Gauge} tone="text-sky-600" value={fmtSpeed(track.vehicle.speed_kmh)} unit="km/h" label="Vitesse" />
      </div>

      {/* Navigation virage par virage (#3D) — pour le chauffeur en course */}
      {(track.status === "in_progress" || track.status === "departed") && track.destination_point && (
        <NavigationSteps
          tripId={track.trip_id}
          current={track.vehicle.latitude && track.vehicle.longitude
            ? [Number(track.vehicle.latitude), Number(track.vehicle.longitude)]
            : null}
          destination={track.destination_point}
          rerouteCount={track.reroute_count ?? 0}
        />
      )}

      {/* Véhicule + chauffeur */}
      <div className="mt-3 flex items-center gap-3 rounded-xl border border-line p-3">
        <span className="flex h-11 w-11 items-center justify-center rounded-full bg-sky-500/10 text-sky-600"><Car className="h-5 w-5" /></span>
        <div className="min-w-0">
          <p className="font-semibold text-ink">{track.vehicle.registration}</p>
          <p className="text-xs text-muted">{track.driver_name ?? "Conduite personnelle"}</p>
        </div>
        <span className="ml-auto text-right text-xs text-muted">
          <span className="flex items-center gap-1">
            <Clock className="h-3 w-3" />
            {fmtSpeed(track.vehicle.speed_kmh) === "—" ? "vitesse —" : `${fmtSpeed(track.vehicle.speed_kmh)} km/h`}
          </span>
        </span>
      </div>

      {/* Étapes */}
      <div className="mt-4 space-y-3">
        {STEPS.map((s, i) => {
          const done = i < cur, active = i === cur;
          return (
            <div key={s.key} className="flex items-center gap-3">
              <span className={cn(
                "flex h-6 w-6 items-center justify-center rounded-full text-[11px] font-bold",
                done ? "bg-emerald-500 text-white" : active ? "bg-brand-500 text-white" : "bg-surface2 text-faint",
              )}>
                {done ? "✓" : i + 1}
              </span>
              <span className={cn("text-sm", active ? "font-semibold text-ink" : done ? "text-muted" : "text-faint")}>{s.label}</span>
            </div>
          );
        })}
      </div>

      {/* Navigation externe + déclaration d'incident (pendant la course) */}
      {inRoute && (
        <div className="mt-3 space-y-2">
          {dest && (
            <div className="grid grid-cols-2 gap-2">
              <a href={wazeUrl(dest[0], dest[1])} target="_blank" rel="noopener noreferrer"
                 className="flex items-center justify-center gap-1.5 rounded-lg border border-line bg-surface2 px-3 py-2 text-xs font-medium text-ink hover:bg-line">
                <ExternalLink className="h-3.5 w-3.5" /> Waze
              </a>
              <a href={googleMapsUrl(dest[0], dest[1])} target="_blank" rel="noopener noreferrer"
                 className="flex items-center justify-center gap-1.5 rounded-lg border border-line bg-surface2 px-3 py-2 text-xs font-medium text-ink hover:bg-line">
                <ExternalLink className="h-3.5 w-3.5" /> Google Maps
              </a>
            </div>
          )}
          <button
            type="button"
            onClick={() => { setIncidentOpen((o) => !o); setIncidentMsg(""); }}
            className="flex w-full items-center justify-center gap-1.5 rounded-lg border border-amber-500/40 bg-amber-500/5 px-3 py-2 text-xs font-medium text-amber-700 hover:bg-amber-500/10"
          >
            <AlertTriangle className="h-3.5 w-3.5" /> Signaler un incident
          </button>
          {incidentOpen && (
            <div className="space-y-2 rounded-lg border border-line p-2.5">
              <Select value={incidentSeverity} onChange={(e) => setIncidentSeverity(e.target.value)} className="h-9 text-xs">
                <option value="minor">Mineur</option>
                <option value="moderate">Modéré</option>
                <option value="major">Majeur</option>
                <option value="critical">Critique</option>
              </Select>
              <textarea
                value={incidentText}
                onChange={(e) => setIncidentText(e.target.value)}
                placeholder="Décrivez l'incident (panne, accident, blocage…)"
                rows={2}
                className="w-full rounded-lg border border-line bg-surface2 px-3 py-2 text-xs text-ink outline-none focus:border-brand-400 focus:ring-2 focus:ring-brand-500/20"
              />
              <Button size="sm" variant="danger" className="w-full" disabled={incidentBusy || !incidentText.trim()} onClick={sendIncident}>
                {incidentBusy ? "…" : "Envoyer le signalement"}
              </Button>
            </div>
          )}
          {incidentMsg && <p className="text-[11px] text-muted">{incidentMsg}</p>}
        </div>
      )}

      {/* Actions de fin de course */}
      {track.status === "in_progress" && (
        <div className="mt-4 space-y-2 rounded-xl border border-line p-3">
          <p className="text-xs font-medium text-muted">Arrivé à destination ?</p>
          <div className="flex gap-2">
            <Input
              type="number"
              min={0}
              placeholder="Km au retour (optionnel)"
              value={endKm}
              onChange={(e) => setEndKm(e.target.value)}
              className="h-9 flex-1 text-xs"
            />
            <Button size="sm" variant="success" disabled={actionBusy} onClick={() => runAction("end")}>
              {actionBusy ? "…" : "Terminer la course"}
            </Button>
          </div>
          <p className="text-[10px] text-faint">Sans kilométrage, la distance est estimée depuis l&apos;itinéraire.</p>
        </div>
      )}
      {track.status === "returned" && (
        <div className="mt-4 rounded-xl border border-emerald-500/30 bg-emerald-500/5 p-3">
          <p className="mb-2 text-xs text-muted">Course terminée — clôturez-la pour libérer le véhicule définitivement.</p>
          <Button size="sm" className="w-full" disabled={actionBusy} onClick={() => runAction("close")}>
            {actionBusy ? "…" : "Clôturer la course"}
          </Button>
        </div>
      )}
      {actionError && (
        <p className="mt-2 rounded-lg bg-rose-500/10 px-3 py-2 text-xs text-rose-600">{actionError}</p>
      )}

      <div className="mt-4 flex gap-2">
        <Link href="/trips" className="flex-1 rounded-lg border border-line px-3 py-2 text-center text-sm font-medium text-ink hover:bg-surface2">Détails course</Link>
        {track.driver_name && (
          <button className="flex items-center gap-1.5 rounded-lg bg-brand-600 px-3 py-2 text-sm font-medium text-white hover:bg-brand-700">
            <Navigation className="h-4 w-4" /> Contacter
          </button>
        )}
      </div>
      </div>
    </div>
  );
}

/** Icône de manœuvre selon le type/modificateur OSRM. */
function stepIcon(step: RouteStep) {
  const mod = step.modifier ?? "";
  if (step.type === "arrive") return <MapPin className="h-4 w-4 text-emerald-600" />;
  if (mod.includes("left")) return <CornerUpLeft className="h-4 w-4 text-brand-600" />;
  if (mod.includes("right")) return <CornerUpRight className="h-4 w-4 text-brand-600" />;
  return <Navigation className="h-4 w-4 text-brand-600" />;
}

function fmtDist(m: number): string {
  return m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m)} m`;
}

/** Distance en km, 1 décimale, format FR ; « — » si indisponible. */
function fmtKm(km: number | null | undefined): string {
  if (km == null) return "—";
  return (Math.round(km * 10) / 10).toLocaleString("fr-FR");
}

/** Vitesse instantanée arrondie ; « — » si position GPS non récente. */
function fmtSpeed(speed: string | null | undefined): string {
  if (speed == null || speed === "") return "—";
  const n = Number(speed);
  return Number.isFinite(n) ? String(Math.round(n)) : "—";
}

/** Tuile de mesure clé du suivi (parcouru / restant / vitesse). */
function Metric({
  icon: Icon,
  value,
  unit,
  label,
  tone,
}: {
  icon: React.ElementType;
  value: string;
  unit: string;
  label: string;
  tone: string;
}) {
  return (
    <div className="rounded-xl border border-line bg-surface2/60 p-2.5 text-center">
      <Icon className={cn("mx-auto h-4 w-4", tone)} />
      <p className="mt-1 text-lg font-bold leading-none text-ink">
        {value}
        <span className="text-[11px] font-medium text-muted"> {unit}</span>
      </p>
      <p className="mt-0.5 text-[10px] uppercase tracking-wide text-faint">{label}</p>
    </div>
  );
}

/**
 * Guidage virage par virage (#3D). Calcule l'itinéraire routier depuis la position
 * courante du véhicule jusqu'à la destination via /routes/calculate (OSRM) et affiche
 * les instructions en français. Recalcule au changement de `rerouteCount` (détour détecté
 * côté serveur, #3C) ou à la demande du chauffeur.
 */
function NavigationSteps({
  tripId,
  current,
  destination,
  rerouteCount,
}: {
  tripId: string;
  current: [number, number] | null;
  destination: [number, number];
  rerouteCount: number;
}) {
  const [route, setRoute] = useState<RouteCalculation | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [open, setOpen] = useState(true);
  // Origine du dernier calcul + dernier compteur de recalcul traités (évite de spammer OSRM).
  const lastKey = useRef<string>("");

  const fetchRoute = useCallback(async () => {
    if (!current) return;
    setLoading(true);
    setError("");
    try {
      const { data } = await api.post<RouteCalculation>("/routes/calculate/", {
        origin: current,
        destination,
      });
      setRoute(data);
    } catch (e) {
      setError(apiError(e));
    } finally {
      setLoading(false);
    }
  }, [current, destination]);

  useEffect(() => {
    if (!current) return;
    // Recalcule uniquement au 1er rendu localisé puis à chaque recalcul serveur.
    const key = `${rerouteCount}`;
    if (lastKey.current === key && route) return;
    lastKey.current = key;
    void fetchRoute();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rerouteCount, Boolean(current)]);

  return (
    <div className="mt-3 rounded-xl border border-line">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-2.5 text-left"
        aria-expanded={open}
      >
        <Navigation className="h-4 w-4 shrink-0 text-brand-500" />
        <span className="text-sm font-semibold text-ink">Navigation</span>
        {route && (
          <span className="text-[11px] text-muted">
            {route.distance_km} km · ~{Math.round(route.eta_min)} min
          </span>
        )}
        {rerouteCount > 0 && (
          <span className="rounded-full bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-medium text-amber-600">
            recalculé ×{rerouteCount}
          </span>
        )}
        <ChevronDown className={cn("ml-auto h-4 w-4 text-muted transition-transform", open && "rotate-180")} />
      </button>

      {open && (
        <div className="border-t border-line px-3 py-2">
          {loading && (
            <div className="flex items-center gap-2 py-2 text-xs text-muted">
              <Spinner className="h-4 w-4" /> Calcul de l&apos;itinéraire…
            </div>
          )}
          {error && <p className="py-2 text-xs text-rose-600">{error}</p>}

          {!loading && !error && route && route.steps.length > 0 && (
            <>
              {/* Prochaine manœuvre mise en avant */}
              <div className="mb-2 flex items-start gap-2.5 rounded-lg bg-brand-500/10 p-2.5">
                <span className="mt-0.5 shrink-0">{stepIcon(route.steps[0])}</span>
                <div className="min-w-0">
                  <p className="text-sm font-semibold leading-snug text-ink">{route.steps[0].instruction}</p>
                  <p className="text-[11px] text-muted">{fmtDist(route.steps[0].distance_m)}</p>
                </div>
              </div>
              {/* Suite de l'itinéraire */}
              <ol className="max-h-44 space-y-1 overflow-y-auto">
                {route.steps.slice(1).map((s, i) => (
                  <li key={i} className="flex items-center gap-2.5 rounded-lg px-1.5 py-1 text-xs">
                    <span className="shrink-0">{stepIcon(s)}</span>
                    <span className="min-w-0 flex-1 truncate text-muted">{s.instruction}</span>
                    <span className="shrink-0 text-faint">{fmtDist(s.distance_m)}</span>
                  </li>
                ))}
              </ol>
            </>
          )}

          {!loading && !error && route && route.steps.length === 0 && (
            <p className="py-2 text-xs text-muted">Guidage détaillé indisponible pour ce trajet.</p>
          )}

          <button
            type="button"
            onClick={() => { void fetchRoute(); }}
            disabled={loading || !current}
            className="mt-2 flex items-center gap-1.5 text-xs font-medium text-brand-600 hover:underline disabled:opacity-60"
          >
            <RotateCw className="h-3.5 w-3.5" /> Recalculer l&apos;itinéraire
          </button>
        </div>
      )}
    </div>
  );
}
