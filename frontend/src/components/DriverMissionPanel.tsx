"use client";

import { useState } from "react";
import Link from "next/link";
import {
  Building2, Car, Clock, Flag, MapPin, Navigation, Play, User, ExternalLink,
} from "lucide-react";

import { Button, Spinner } from "@/components/ui";
import { api, apiError } from "@/lib/api";
import { googleMapsUrl, wazeUrl } from "@/lib/driver";
import type { DriverMission, TripRouteData } from "@/lib/types";
import { cn } from "@/lib/utils";

function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("fr-FR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

/**
 * Panneau « mission-first » du chauffeur sur /map : détails de la mission assignée,
 * itinéraire prévu, et bouton « Démarrer la course » (demande GPS + bascule en cours).
 * Pour une course en cours, c'est TrackingPanel (existant) qui prend le relais.
 */
export function DriverMissionPanel({
  mission,
  route,
  onStarted,
}: {
  mission: DriverMission;
  route?: TripRouteData | null;
  onStarted: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const res = mission.reservation;
  const dest = mission.route?.destination_point ?? route?.destination_point ?? null;
  const distance = route?.distance_km ?? mission.route?.distance_km ?? null;
  const duration = route?.duration_min ?? mission.route?.duration_min ?? null;

  async function start() {
    if (busy) return;
    setBusy(true);
    setError("");
    // Demande l'autorisation GPS (le tracking temps réel démarrera après le départ).
    if (typeof navigator !== "undefined" && navigator.geolocation && window.isSecureContext) {
      await new Promise<void>((resolve) =>
        navigator.geolocation.getCurrentPosition(() => resolve(), () => resolve(), { timeout: 6000, maximumAge: 60_000 }),
      );
    }
    try {
      await api.post(`/trips/${mission.trip_id}/start/`, {});
      onStarted();
    } catch (e) {
      setError(apiError(e));
      setBusy(false);
    }
  }

  return (
    <div className="absolute inset-x-2 bottom-2 z-[500] flex max-h-[70vh] flex-col overflow-hidden rounded-2xl border border-line bg-surface/95 shadow-2xl backdrop-blur lg:inset-x-auto lg:bottom-auto lg:right-4 lg:top-4 lg:w-[24rem]">
      <div className="flex items-center gap-2.5 bg-gradient-to-r from-navy-800 to-navy-900 px-4 py-3 text-white">
        <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-brand-500/20"><Navigation className="h-4 w-4 text-brand-400" /></span>
        <div className="leading-tight">
          <p className="text-sm font-semibold">Ma mission</p>
          <p className="text-[11px] text-slate-300">{mission.status_display}</p>
        </div>
        {res?.id && (
          <span className="ml-auto rounded-md bg-white/10 px-2 py-0.5 font-mono text-[11px]">
            N° {res.id.slice(0, 8).toUpperCase()}
          </span>
        )}
      </div>

      <div className="flex-1 space-y-3 overflow-y-auto px-4 py-3">
        {/* Trajet */}
        <div className="space-y-1.5 text-sm">
          <p className="flex items-start gap-2"><MapPin className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600" /><span className="text-ink">{res?.origin || "Départ non précisé"}</span></p>
          <p className="flex items-start gap-2"><Flag className="mt-0.5 h-4 w-4 shrink-0 text-brand-600" /><span className="font-medium text-ink">{mission.destination}</span></p>
        </div>

        {/* Estimations */}
        {(distance != null || duration != null) && (
          <div className="grid grid-cols-2 gap-2 rounded-xl bg-surface2 p-2.5 text-center">
            <div><p className="text-sm font-bold text-ink">{distance != null ? `${distance} km` : "—"}</p><p className="text-[10px] text-faint">distance estimée</p></div>
            <div><p className="text-sm font-bold text-ink">{duration != null ? `~${Math.round(duration)} min` : "—"}</p><p className="text-[10px] text-faint">durée estimée</p></div>
          </div>
        )}

        {/* Détails */}
        <dl className="space-y-1.5 text-xs">
          <Info icon={Clock} label="Départ prévu" value={fmtTime(res?.departure_time ?? null)} />
          <Info icon={Clock} label="Retour estimé" value={fmtTime(res?.estimated_return ?? null)} />
          <Info icon={Car} label="Véhicule" value={mission.vehicle ? `${mission.vehicle.registration} · ${mission.vehicle.label ?? ""}` : "—"} />
          <Info icon={User} label="Demandeur" value={res?.requester_name ?? "—"} />
          <Info icon={Building2} label="Filiale" value={mission.subsidiary_name ?? "—"} />
          {res?.passengers != null && <Info icon={User} label="Passagers" value={String(res.passengers)} />}
        </dl>

        {error && <p className="rounded-lg bg-rose-500/10 px-3 py-2 text-xs text-rose-600">{error}</p>}

        {/* Navigation externe */}
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
      </div>

      {/* Action principale */}
      <div className="space-y-2 border-t border-line p-3">
        {mission.can_start ? (
          <Button className="w-full" disabled={busy} onClick={start}>
            {busy ? <Spinner className="h-4 w-4 border-white/40 border-t-white" /> : <><Play className="h-4 w-4" /> Démarrer la course</>}
          </Button>
        ) : (
          <p className="text-center text-xs text-muted">En attente de l'heure de départ ou d'une action du gestionnaire.</p>
        )}
        <Link href="/driver" className="block text-center text-[11px] text-brand-600 hover:underline">
          Voir toutes mes missions
        </Link>
      </div>
    </div>
  );
}

function Info({ icon: Icon, label, value }: { icon: React.ElementType; label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-3 border-b border-line/60 py-1.5 last:border-0">
      <span className="flex items-center gap-1.5 text-muted"><Icon className="h-3.5 w-3.5" />{label}</span>
      <span className={cn("truncate text-right font-medium text-ink")}>{value}</span>
    </div>
  );
}

/** État vide : chauffeur sans mission assignée. */
export function DriverNoMissionPanel() {
  return (
    <div className="absolute inset-x-2 bottom-2 z-[500] rounded-2xl border border-line bg-surface/95 p-5 text-center shadow-2xl backdrop-blur lg:inset-x-auto lg:bottom-auto lg:right-4 lg:top-4 lg:w-[24rem]">
      <span className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-emerald-500/10 text-emerald-600"><Navigation className="h-6 w-6" /></span>
      <p className="text-sm font-semibold text-ink">Aucune mission assignée</p>
      <p className="mt-1 text-xs text-muted">Vous serez notifié dès qu'une course vous sera affectée.</p>
      <Link href="/driver" className="mt-3 inline-block text-xs font-medium text-brand-600 hover:underline">Mon espace chauffeur</Link>
    </div>
  );
}
