"use client";

import { useMemo, useRef, useState } from "react";
import dynamic from "next/dynamic";
import Link from "next/link";
import {
  AlertTriangle,
  Car,
  ClipboardList,
  Clock,
  Crosshair,
  Fuel,
  Gauge,
  Maximize,
  Maximize2,
  Minimize,
  Minimize2,
  MapPin,
  Phone,
  Route as RouteIcon,
  Search,
  UserRound,
} from "lucide-react";

import { Button, Card, CardBody, Input, Spinner } from "@/components/ui";
import { StatusBadge } from "@/components/StatusBadge";
import {
  AssignDriverModal,
  AssignVehicleModal,
  RejectModal,
} from "@/components/reservation-modals";
import {
  useFuelIntel,
  useGeofenceZones,
  useReservationAction,
  useReservations,
  useTripRoute,
} from "@/lib/queries";
import { useAuth } from "@/lib/auth";
import { useFleetLive } from "@/lib/useFleetLive";
import { useFullscreen } from "@/lib/useFullscreen";
import { useSubsidiaryFilter } from "@/lib/subsidiary";
import { apiError } from "@/lib/api";
import type { Reservation } from "@/lib/types";
import { cn, formatDate } from "@/lib/utils";

const MapView = dynamic(() => import("@/components/MapView"), {
  ssr: false,
  loading: () => (
    <div className="flex h-full items-center justify-center bg-surface2">
      <Spinner className="h-7 w-7" />
    </div>
  ),
});

type ModalState =
  | { type: "reject"; res: Reservation }
  | { type: "assign-vehicle"; res: Reservation }
  | { type: "assign-driver"; res: Reservation }
  | null;

const ACTIONABLE = ["submitted", "pending_manager", "pending_fleet", "approved", "vehicle_assigned", "driver_assigned"];

export default function FleetControlPage() {
  const { me } = useAuth();
  const { selected: subFilter } = useSubsidiaryFilter();
  const { positions, connected, isLoading } = useFleetLive(subFilter || undefined);
  const [tab, setTab] = useState<"vehicles" | "requests" | "fuel">("vehicles");
  // Visibilité des coûts : gestionnaires de flotte / admins / finance uniquement.
  const canSeeFuel = Boolean(
    me?.has_company_scope || ["fleet_manager", "subsidiary_admin", "finance"].includes(me?.role ?? ""),
  );
  const fuelIntel = useFuelIntel(canSeeFuel);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [follow, setFollow] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [modal, setModal] = useState<ModalState>(null);
  const [toast, setToast] = useState("");

  const mapWrapRef = useRef<HTMLDivElement>(null);
  const { isFullscreen, toggle: toggleFullscreen } = useFullscreen(mapWrapRef);

  const toggleExpand = () => {
    setExpanded((v) => !v);
    setTimeout(() => window.dispatchEvent(new Event("resize")), 250);
  };

  const selectedTripId = positions.find((p) => p.id === selectedId)?.trip_id ?? null;
  const { data: route } = useTripRoute(selectedTripId);
  const { data: zones } = useGeofenceZones();

  // Demandes à traiter (validation + affectation)
  const { data: resData } = useReservations({ page_size: "100" });
  const requests = useMemo(
    () => (resData?.results ?? []).filter((r) => ACTIONABLE.includes(r.status)),
    [resData],
  );

  const approve = useReservationAction("approve");
  const runApprove = (res: Reservation) =>
    approve.mutate({ id: res.id }, { onError: (e) => setToast(apiError(e)) });

  const filtered = useMemo(
    () =>
      positions.filter((p) => {
        if (statusFilter === "late") {
          if (!p.is_late) return false;
        } else if (statusFilter && p.status !== statusFilter) return false;
        return !search || p.registration.toLowerCase().includes(search.toLowerCase());
      }),
    [positions, search, statusFilter],
  );

  const selected = positions.find((p) => p.id === selectedId) ?? null;
  const stats = [
    { key: "on_trip", label: "En course", value: positions.filter((p) => p.status === "on_trip").length, icon: RouteIcon, tone: "text-sky-600" },
    { key: "available", label: "Disponibles", value: positions.filter((p) => p.status === "available").length, icon: MapPin, tone: "text-emerald-600" },
    { key: "late", label: "En retard", value: positions.filter((p) => p.is_late).length, icon: Clock, tone: "text-rose-600" },
    { key: "maintenance", label: "Maintenance", value: positions.filter((p) => p.status === "maintenance").length, icon: AlertTriangle, tone: "text-amber-600" },
  ];

  // Suivi : la carte reste centrée sur le véhicule sélectionné à chaque mise à jour GPS.
  const followTo: [number, number] | null =
    follow && selected?.latitude
      ? [Number(selected.latitude), Number(selected.longitude)]
      : null;

  return (
    <div className="space-y-4">
      {!expanded && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          {stats.map((s) => (
            <button
              key={s.key}
              onClick={() => { setStatusFilter(statusFilter === s.key ? "" : s.key); setTab("vehicles"); }}
              className="text-left"
            >
              <Card className={cn("transition-all hover:shadow-md", statusFilter === s.key && "ring-2 ring-brand-500")}>
                <CardBody className="flex items-center gap-3 py-3">
                  <s.icon className={cn("h-5 w-5", s.tone)} />
                  <div>
                    <p className="text-xl font-semibold text-ink">{s.value}</p>
                    <p className="text-[11px] text-muted">{s.label}</p>
                  </div>
                </CardBody>
              </Card>
            </button>
          ))}
        </div>
      )}

      {toast && (
        <div className="flex items-center justify-between rounded-lg bg-rose-500/10 px-4 py-2 text-sm text-rose-600">
          {toast}
          <button onClick={() => setToast("")} className="text-rose-400 hover:text-rose-600">✕</button>
        </div>
      )}

      <div className={cn("grid gap-4", !expanded && "lg:grid-cols-[22rem_1fr]")}>
        {!expanded && (
          <Card className="order-2 flex max-h-[70vh] flex-col lg:order-1">
            {/* Onglets */}
            <div className="flex border-b border-line">
              <button
                onClick={() => setTab("vehicles")}
                className={cn(
                  "flex flex-1 items-center justify-center gap-1.5 px-3 py-2.5 text-xs font-semibold transition-colors",
                  tab === "vehicles" ? "border-b-2 border-brand-500 text-brand-600" : "text-muted hover:text-ink",
                )}
              >
                <Car className="h-3.5 w-3.5" /> Véhicules ({positions.length})
              </button>
              <button
                onClick={() => setTab("requests")}
                className={cn(
                  "flex flex-1 items-center justify-center gap-1.5 px-3 py-2.5 text-xs font-semibold transition-colors",
                  tab === "requests" ? "border-b-2 border-brand-500 text-brand-600" : "text-muted hover:text-ink",
                )}
              >
                <ClipboardList className="h-3.5 w-3.5" /> Demandes
                {requests.length > 0 && (
                  <span className="rounded-full bg-brand-500 px-1.5 text-[10px] font-bold text-white">{requests.length}</span>
                )}
              </button>
              {canSeeFuel && (
                <button
                  onClick={() => setTab("fuel")}
                  className={cn(
                    "flex flex-1 items-center justify-center gap-1.5 px-3 py-2.5 text-xs font-semibold transition-colors",
                    tab === "fuel" ? "border-b-2 border-brand-500 text-brand-600" : "text-muted hover:text-ink",
                  )}
                >
                  <Fuel className="h-3.5 w-3.5" /> Carburant
                </button>
              )}
            </div>

            {tab === "vehicles" ? (
              <>
                <div className="border-b border-line p-3">
                  <div className="relative">
                    <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-faint" />
                    <Input placeholder="Immatriculation…" value={search} onChange={(e) => setSearch(e.target.value)} className="pl-9" />
                  </div>
                </div>
                <div className="flex-1 overflow-y-auto">
                  {isLoading ? (
                    <div className="flex justify-center py-10"><Spinner className="h-6 w-6" /></div>
                  ) : (
                    filtered.map((p) => (
                      <button
                        key={p.id}
                        onClick={() => setSelectedId(p.id)}
                        className={cn(
                          "flex w-full items-center justify-between gap-2 border-b border-line px-4 py-3 text-left hover:bg-surface2",
                          selectedId === p.id && "bg-brand-500/5",
                        )}
                      >
                        <div className="min-w-0">
                          <p className="truncate text-sm font-medium text-ink">{p.registration}</p>
                          <p className="truncate text-xs text-muted">{p.driver_name ?? `${p.brand} ${p.model}`}</p>
                        </div>
                        <div className="flex flex-col items-end gap-1">
                          <StatusBadge code={p.status} label={p.status_display} />
                          {p.is_late && <span className="text-[10px] font-medium text-rose-600">En retard</span>}
                        </div>
                      </button>
                    ))
                  )}
                </div>
              </>
            ) : tab === "requests" ? (
              <div className="flex-1 overflow-y-auto">
                {requests.length === 0 ? (
                  <p className="py-10 text-center text-sm text-faint">Aucune demande à traiter 🎉</p>
                ) : (
                  requests.map((r) => (
                    <div key={r.id} className="border-b border-line px-4 py-3">
                      <div className="flex items-start justify-between gap-2">
                        <div className="min-w-0">
                          <p className="truncate text-sm font-medium text-ink">{r.destination}</p>
                          <p className="truncate text-[11px] text-muted">
                            {r.requester_name} · {formatDate(r.departure_time, true)}
                          </p>
                        </div>
                        <StatusBadge code={r.status} label={r.status_display} />
                      </div>
                      {(r.vehicle_registration || r.driver_name) && (
                        <div className="mt-1.5 flex flex-wrap gap-1.5">
                          {r.vehicle_registration && (
                            <span className="inline-flex items-center gap-1 rounded-md bg-sky-500/10 px-1.5 py-0.5 text-[10px] font-medium text-sky-600">
                              <Car className="h-2.5 w-2.5" /> {r.vehicle_registration}
                            </span>
                          )}
                          {r.driver_name && (
                            <span className="inline-flex items-center gap-1 rounded-md bg-violet-500/10 px-1.5 py-0.5 text-[10px] font-medium text-violet-600">
                              <UserRound className="h-2.5 w-2.5" /> {r.driver_name}
                            </span>
                          )}
                        </div>
                      )}
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        {["pending_manager", "pending_fleet"].includes(r.status) && (
                          <>
                            <Button size="sm" variant="success" disabled={approve.isPending} onClick={() => runApprove(r)}>
                              Valider
                            </Button>
                            <Button size="sm" variant="danger" onClick={() => setModal({ type: "reject", res: r })}>
                              Refuser
                            </Button>
                          </>
                        )}
                        {r.status === "approved" && (
                          <Button size="sm" onClick={() => setModal({ type: "assign-vehicle", res: r })}>
                            Affecter véhicule
                          </Button>
                        )}
                        {r.status === "vehicle_assigned" && r.needs_driver && (
                          <Button size="sm" onClick={() => setModal({ type: "assign-driver", res: r })}>
                            Affecter chauffeur
                          </Button>
                        )}
                        {(r.status === "driver_assigned" || (r.status === "vehicle_assigned" && !r.needs_driver)) && (
                          <span className="inline-flex items-center rounded-full bg-emerald-500/10 px-2.5 py-1 text-[11px] font-medium text-emerald-600">
                            ✓ Prête au départ
                          </span>
                        )}
                        <Link
                          href={`/reservations/${r.id}`}
                          className="ml-auto inline-flex items-center rounded-md px-2 py-1 text-[11px] font-medium text-muted hover:bg-surface2 hover:text-ink"
                        >
                          Détails →
                        </Link>
                      </div>
                    </div>
                  ))
                )}
              </div>
            ) : (
              <FuelIntelPanel data={fuelIntel.data} loading={fuelIntel.isLoading} />
            )}
          </Card>
        )}

        <div className="order-1 lg:order-2">
          <div
            ref={mapWrapRef}
            className={cn(
              "kx-mapfs relative overflow-hidden rounded-[var(--radius-card)] border border-line bg-surface shadow-sm",
              isFullscreen ? "h-screen" : expanded ? "h-[86vh]" : "h-[70vh]",
            )}
          >
            <MapView
              positions={filtered}
              selectedId={selectedId}
              onSelect={setSelectedId}
              planned={route?.planned}
              actual={route?.actual}
              destination={route?.destination_point}
              zones={zones}
              recenterTo={followTo}
            />

            {/* Contrôles d'affichage : suivre + élargir + plein écran */}
            <div className="absolute right-3 top-12 z-[600] flex gap-1.5">
              <button
                onClick={() => setFollow((v) => !v)}
                title={follow ? "Arrêter le suivi" : "Suivre le véhicule sélectionné"}
                className={cn(
                  "flex h-8 w-8 items-center justify-center rounded-lg border border-line shadow backdrop-blur",
                  follow ? "bg-brand-600 text-white" : "bg-surface/90 text-muted hover:bg-surface2 hover:text-ink",
                )}
              >
                <Crosshair className="h-4 w-4" />
              </button>
              <button
                onClick={toggleExpand}
                title={expanded ? "Réduire" : "Élargir la carte"}
                className="flex h-8 w-8 items-center justify-center rounded-lg border border-line bg-surface/90 text-muted shadow backdrop-blur hover:bg-surface2 hover:text-ink"
              >
                {expanded ? <Minimize2 className="h-4 w-4" /> : <Maximize2 className="h-4 w-4" />}
              </button>
              <button
                onClick={toggleFullscreen}
                title={isFullscreen ? "Quitter le plein écran" : "Plein écran"}
                className="flex h-8 w-8 items-center justify-center rounded-lg border border-line bg-surface/90 text-muted shadow backdrop-blur hover:bg-surface2 hover:text-ink"
              >
                {isFullscreen ? <Minimize className="h-4 w-4" /> : <Maximize className="h-4 w-4" />}
              </button>
            </div>

            {/* Indicateur temps réel */}
            <div className="absolute right-3 top-3 z-[500] flex items-center gap-1.5 rounded-full border border-line bg-surface/90 px-3 py-1 text-xs font-medium shadow backdrop-blur">
              <span className={cn("h-2 w-2 rounded-full", connected ? "animate-pulse bg-emerald-500" : "bg-amber-500")} />
              {connected ? "Temps réel" : "Reconnexion…"}
            </div>

            {route && route.distance_km > 0 && (
              <div className="absolute left-3 top-3 z-[500] w-56 rounded-xl border border-line bg-surface/95 p-3 shadow-lg backdrop-blur">
                <div className="mb-2 flex items-center gap-1.5">
                  <RouteIcon className="h-4 w-4 text-brand-500" />
                  <p className="truncate text-xs font-semibold text-ink">{route.destination}</p>
                </div>
                <div className="flex items-end justify-between">
                  <div>
                    <p className="text-lg font-bold leading-none text-ink">{route.distance_km.toFixed(1)} km</p>
                    <p className="text-[10px] text-faint">distance totale</p>
                  </div>
                  {route.duration_min != null && (
                    <div className="text-right">
                      <p className="text-sm font-semibold leading-none text-ink">~{Math.round(route.duration_min)} min</p>
                      <p className="text-[10px] text-faint">durée est.</p>
                    </div>
                  )}
                </div>
                <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-surface2">
                  <div className="h-full rounded-full bg-brand-500 transition-all" style={{ width: `${Math.round(route.progress * 100)}%` }} />
                </div>
                <div className="mt-1.5 flex justify-between text-[10px] text-muted">
                  <span>{route.traveled_km.toFixed(1)} km parcourus</span>
                  <span>{route.remaining_km.toFixed(1)} km restants</span>
                </div>
                <div className="mt-2 flex items-center justify-between border-t border-line pt-2 text-[11px]">
                  <span className="flex items-center gap-1 text-muted">
                    <Gauge className="h-3.5 w-3.5" /> {route.speed_kmh != null ? `${route.speed_kmh} km/h` : "—"}
                  </span>
                  <span className="flex items-center gap-2">
                    <span className="flex items-center gap-1 text-muted"><span className="inline-block h-1 w-4 rounded bg-[#3b82f6]" />Prévu</span>
                    <span className="flex items-center gap-1 text-muted"><span className="inline-block h-1 w-4 rounded bg-[#f97316]" />Réel</span>
                  </span>
                </div>
              </div>
            )}

            {selected && (
              <div className="animate-pop absolute bottom-4 left-4 right-4 z-[500] mx-auto max-w-md rounded-xl border border-line bg-surface/95 p-4 shadow-xl backdrop-blur">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <p className="font-semibold text-ink">{selected.registration}</p>
                    <p className="text-xs text-muted">{selected.brand} {selected.model} · {selected.subsidiary_name}</p>
                  </div>
                  <StatusBadge code={selected.status} label={selected.status_display} />
                </div>
                <div className="mt-2 grid grid-cols-2 gap-1.5 text-xs text-muted">
                  <span>Chauffeur : {selected.driver_name ?? "—"}</span>
                  <span>Destination : {selected.destination ?? "—"}</span>
                  <span className="flex items-center gap-1">
                    <Gauge className="h-3.5 w-3.5" /> {selected.speed_kmh ? `${selected.speed_kmh} km/h` : "—"}
                  </span>
                  <span>Distance : {route ? `${route.traveled_km.toFixed(1)} / ${route.distance_km.toFixed(1)} km` : "—"}</span>
                  <span className="col-span-2">MAJ : {formatDate(selected.recorded_at, true)}</span>
                </div>
                <div className="mt-3 flex gap-2">
                  <button
                    onClick={() => setFollow((v) => !v)}
                    className={cn(
                      "flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium",
                      follow ? "bg-brand-600 text-white" : "border border-line text-muted hover:bg-surface2",
                    )}
                  >
                    <Crosshair className="h-3.5 w-3.5" /> {follow ? "Suivi actif" : "Suivre"}
                  </button>
                  <button className="flex items-center gap-1.5 rounded-lg border border-line px-3 py-1.5 text-xs text-muted hover:bg-surface2">
                    <Phone className="h-3.5 w-3.5" /> Contacter
                  </button>
                  {selected.trip_id && (
                    <Link href="/trips" className="flex items-center gap-1.5 rounded-lg bg-brand-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-brand-700">
                      <RouteIcon className="h-3.5 w-3.5" /> Fiche course
                    </Link>
                  )}
                </div>
              </div>
            )}
          </div>
        </div>
      </div>

      {modal?.type === "reject" && <RejectModal res={modal.res} onClose={() => setModal(null)} onError={setToast} />}
      {modal?.type === "assign-vehicle" && <AssignVehicleModal res={modal.res} onClose={() => setModal(null)} onError={setToast} />}
      {modal?.type === "assign-driver" && <AssignDriverModal res={modal.res} onClose={() => setModal(null)} onError={setToast} />}
    </div>
  );
}

/** Fleet Fuel Intelligence — indicateurs carburant (gestionnaires uniquement). */
function FuelIntelPanel({ data, loading }: { data?: import("@/lib/queries").FuelIntelData; loading: boolean }) {
  if (loading || !data) {
    return <div className="flex flex-1 items-center justify-center py-10"><Spinner className="h-6 w-6" /></div>;
  }
  const fmt = (n: number) => n.toLocaleString("fr-FR");
  return (
    <div className="flex-1 space-y-3 overflow-y-auto p-3 text-xs">
      {/* Conso & coûts */}
      <div className="grid grid-cols-2 gap-2">
        {[
          { label: "Conso. du jour", value: `${fmt(data.day.liters)} L`, sub: `${fmt(data.day.cost)} XOF` },
          { label: "Conso. du mois", value: `${fmt(data.month.liters)} L`, sub: `${fmt(data.month.cost)} XOF` },
          { label: "Prévision mensuelle", value: `${fmt(data.forecast.liters)} L`, sub: `${fmt(data.forecast.cost)} XOF` },
          { label: "Écart prévu / réel", value: data.gap_pct != null ? `${data.gap_pct > 0 ? "+" : ""}${data.gap_pct}%` : "—", sub: data.fleet_rate ? `flotte ${data.fleet_rate} L/100km` : "" },
        ].map((k) => (
          <div key={k.label} className="rounded-lg border border-line p-2">
            <p className="text-[10px] text-faint">{k.label}</p>
            <p className="text-sm font-bold text-ink">{k.value}</p>
            {k.sub && <p className="text-[10px] text-muted">{k.sub}</p>}
          </div>
        ))}
      </div>

      {/* Alertes surconsommation */}
      {data.overconsumption.length > 0 && (
        <div className="rounded-lg border border-rose-500/30 bg-rose-500/5 p-2">
          <p className="mb-1 font-semibold text-rose-600">⚠ Surconsommation détectée</p>
          {data.overconsumption.map((o) => (
            <p key={o.label} className="text-muted">{o.label} : {o.rate} L/100km (+{o.excess_pct}% vs flotte)</p>
          ))}
        </div>
      )}

      {/* Tops */}
      <div>
        <p className="mb-1 font-semibold uppercase tracking-wide text-faint">Top véhicules consommateurs</p>
        {data.top_vehicles.length === 0 ? <p className="text-faint">Pas encore de données.</p> :
          data.top_vehicles.map((v) => (
            <div key={v.label} className="flex justify-between border-b border-line py-1 last:border-0">
              <span className="text-ink">{v.label}</span>
              <span className="text-muted">{v.rate} L/100km · {v.samples} course(s)</span>
            </div>
          ))}
      </div>
      <div>
        <p className="mb-1 font-semibold uppercase tracking-wide text-faint">Chauffeurs les plus économes</p>
        {data.top_drivers.length === 0 ? <p className="text-faint">Pas encore de données.</p> :
          data.top_drivers.map((d) => (
            <div key={d.label} className="flex justify-between border-b border-line py-1 last:border-0">
              <span className="text-ink">{d.label}</span>
              <span className="text-emerald-600">{d.rate} L/100km</span>
            </div>
          ))}
      </div>

      {/* Prix carburant CI */}
      <div>
        <p className="mb-1 font-semibold uppercase tracking-wide text-faint">Prix carburant (Côte d'Ivoire)</p>
        {Object.entries(data.prices).map(([code, p]) => (
          <div key={code} className="flex items-center justify-between border-b border-line py-1 last:border-0">
            <span className="text-ink">{p.label}</span>
            <span className="text-muted">
              {p.price != null ? `${fmt(p.price)} XOF/L` : "—"}
              {p.date && <span className="text-faint"> · maj {p.date}</span>}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
