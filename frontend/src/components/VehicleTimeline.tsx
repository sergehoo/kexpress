"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { AlertTriangle, Car, ChevronLeft, ChevronRight, CornerUpLeft, Inbox } from "lucide-react";

import { Card, CardBody, EmptyState, Spinner } from "@/components/ui";
import { apiError } from "@/lib/api";
import { useReservationAction, useTripAction, useVehicles } from "@/lib/queries";
import type { Reservation } from "@/lib/types";
import { cn } from "@/lib/utils";

type Range = "day" | "week";

// Échelles d'affichage.
const DAY_START_H = 6;
const DAY_END_H = 22;
const HOURS = DAY_END_H - DAY_START_H;
const PX_PER_HOUR = 60;
const PX_PER_DAY = 150; // vue semaine
const LABEL_W = 184;
const ROW_H = 52;

// Statuts de COURSE pour lesquels (ré)affecter un véhicule est possible (cf. services.assign_vehicle_to_trip).
const REASSIGNABLE = new Set(["scheduled"]);
// Ignorés : n'occupent pas un véhicule (course annulée, réservation rejetée/brouillon).
const HIDDEN = new Set(["draft", "rejected", "cancelled"]);
// Pris en compte pour la détection de conflits (occupation réelle du véhicule).
const TERMINAL = new Set(["completed", "closed"]);
// Statuts de RÉSERVATION dont les horaires peuvent encore être replanifiés (cf. RESCHEDULABLE_STATUSES backend).
const RESCHEDULABLE = new Set([
  "submitted", "pending_manager", "pending_fleet", "approved", "vehicle_assigned", "driver_assigned",
]);
const SNAP_MS = 15 * 60 * 1000; // pas de 15 min
const MIN_DUR_MS = 30 * 60 * 1000; // durée minimale d'une course

const TONE: Record<string, string> = {
  scheduled: "bg-violet-500/15 text-violet-700 ring-violet-500/30",
  departed: "bg-sky-500/15 text-sky-700 ring-sky-500/30",
  in_progress: "bg-emerald-500/15 text-emerald-700 ring-emerald-500/30",
  returned: "bg-amber-500/15 text-amber-700 ring-amber-500/30",
  completed: "bg-slate-400/20 text-slate-600 ring-slate-400/30",
  closed: "bg-slate-400/20 text-slate-600 ring-slate-400/30",
};

/** Un segment (aller / retour) projeté sur le planning : sa propre ligne de temps, son propre
 *  véhicule, son propre statut — une réservation aller-retour donne DEUX barres distinctes. */
type LegItem = {
  id: string; // identifiant de la course (Trip)
  resId: string;
  leg: "outbound" | "return";
  isRound: boolean;
  vehicle: string | null;
  status: string;
  depMs: number;
  retMs: number;
  destination: string;
  res: Reservation; // back-ref pour la replanification (endpoint réservation)
};

function startOfDay(d: Date) { const x = new Date(d); x.setHours(0, 0, 0, 0); return x; }
function startOfWeek(d: Date) {
  const x = startOfDay(d);
  const dow = (x.getDay() + 6) % 7; // lundi = 0
  x.setDate(x.getDate() - dow);
  return x;
}
function fmtTime(d: Date) { return d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" }); }
function ms(s: string) { return new Date(s).getTime(); }
function legOverlaps(a: LegItem, b: LegItem) {
  return a.depMs < b.retMs && b.depMs < a.retMs;
}

/** Aplatit les réservations en segments : une barre par course, positionnée sur SON véhicule et
 *  SES horaires prévus. Les courses annulées et les réservations rejetées/brouillon sont exclues. */
function buildLegs(reservations: Reservation[]): LegItem[] {
  const items: LegItem[] = [];
  for (const r of reservations) {
    if (HIDDEN.has(r.status)) continue;
    const isRound = r.trip_type === "round_trip";
    for (const t of r.trips) {
      if (HIDDEN.has(t.status)) continue; // course annulée
      const dep = t.planned_departure_at ? ms(t.planned_departure_at) : ms(r.departure_time);
      const ret = t.planned_arrival_at ? ms(t.planned_arrival_at) : ms(r.estimated_return);
      items.push({
        id: t.id, resId: r.id, leg: t.leg, isRound, vehicle: t.vehicle, status: t.status,
        depMs: dep, retMs: ret, destination: t.destination, res: r,
      });
    }
  }
  return items;
}

/** Traduit le redimensionnement d'un SEGMENT en une replanification de la réservation
 *  (l'endpoint reschedule est réservation-global ; le backend recalcule les horaires prévus
 *  de chaque course). Aller : départ / (A-R : arrivée = départ du retour). Retour : départ du
 *  retour / fin de mission. Les champs non touchés conservent la valeur courante. */
function reschedulePayloadForLeg(item: LegItem, depMs: number, retMs: number) {
  const r = item.res;
  let departure = ms(r.departure_time);
  let estimated = ms(r.estimated_return);
  let ret: number | undefined = r.return_time ? ms(r.return_time) : undefined;
  if (!item.isRound) {
    departure = depMs; estimated = retMs;
  } else if (item.leg === "outbound") {
    departure = depMs; ret = retMs;
  } else {
    ret = depMs; estimated = retMs;
  }
  const body: Record<string, string> = {
    departure_time: new Date(departure).toISOString(),
    estimated_return: new Date(estimated).toISOString(),
  };
  if (ret !== undefined) body.return_time = new Date(ret).toISOString();
  return body;
}

/**
 * Planning d'occupation des véhicules : une ligne par véhicule, le temps en abscisse
 * (jour ou semaine). Chaque COURSE (aller / retour) est une barre distincte, sur la ligne de
 * SON véhicule et à SES horaires prévus — un aller-retour peut donc apparaître sur deux
 * véhicules différents. Glisser-déposer une course d'une ligne à une autre pour (ré)affecter
 * SON véhicule ; les chevauchements sont surlignés en rouge et signalés avant le dépôt (la
 * validation finale des conflits reste côté serveur, par segment).
 */
export function VehicleTimeline({
  reservations,
  onError,
}: {
  reservations: Reservation[];
  onError?: (msg: string) => void;
}) {
  const router = useRouter();
  const [range, setRange] = useState<Range>("day");
  const [anchor, setAnchor] = useState(() => startOfDay(new Date()));
  const { data: vData, isLoading } = useVehicles({ page_size: "200" });
  const assign = useTripAction("assign-vehicle"); // affectation PAR COURSE
  const reschedule = useReservationAction("reschedule");
  const [dragId, setDragId] = useState<string | null>(null);
  const [overVeh, setOverVeh] = useState<string | null>(null);
  // Redimensionnement d'une barre (ajuster départ/retour) — souris native (pas DnD HTML5).
  type ResizeState = { id: string; side: "start" | "end"; startX: number; origDep: number; origRet: number };
  const [resize, setResize] = useState<ResizeState | null>(null);
  const [preview, setPreview] = useState<{ id: string; dep: number; ret: number } | null>(null);
  const resizeRef = useRef<ResizeState | null>(null);
  const pendingRef = useRef<{ dep: number; ret: number } | null>(null);
  const justResized = useRef(false);

  const vehicles = useMemo(() => vData?.results ?? [], [vData]);
  const isWeek = range === "week";

  const legs = useMemo(() => buildLegs(reservations), [reservations]);

  // Fenêtre + graduations selon l'échelle.
  const { winStart, winEnd, gridW, cols, ticks } = useMemo(() => {
    if (isWeek) {
      const s = startOfWeek(anchor);
      const e = new Date(s); e.setDate(e.getDate() + 7);
      const t = Array.from({ length: 7 }).map((_, i) => {
        const d = new Date(s); d.setDate(d.getDate() + i);
        return { left: i * PX_PER_DAY, label: d.toLocaleDateString("fr-FR", { weekday: "short", day: "2-digit", month: "2-digit" }) };
      });
      return { winStart: s, winEnd: e, gridW: 7 * PX_PER_DAY, cols: 7, ticks: t };
    }
    const s = new Date(anchor); s.setHours(DAY_START_H, 0, 0, 0);
    const e = new Date(anchor); e.setHours(DAY_END_H, 0, 0, 0);
    const t = Array.from({ length: HOURS + 1 }).map((_, i) => ({ left: i * PX_PER_HOUR, label: `${String(DAY_START_H + i).padStart(2, "0")}h` }));
    return { winStart: s, winEnd: e, gridW: HOURS * PX_PER_HOUR, cols: HOURS, ticks: t };
  }, [anchor, isWeek]);

  const winMs = winEnd.getTime() - winStart.getTime();
  const colW = gridW / cols;

  // Segments croisant la fenêtre.
  const dayLegs = useMemo(
    () => legs.filter((l) => l.retMs > winStart.getTime() && l.depMs < winEnd.getTime()),
    [legs, winStart, winEnd],
  );

  const byVehicle = useMemo(() => {
    const m = new Map<string, LegItem[]>();
    for (const l of dayLegs) {
      if (!l.vehicle) continue;
      const arr = m.get(l.vehicle) ?? [];
      arr.push(l);
      m.set(l.vehicle, arr);
    }
    return m;
  }, [dayLegs]);

  const unassigned = useMemo(() => dayLegs.filter((l) => !l.vehicle && REASSIGNABLE.has(l.status)), [dayLegs]);

  // Conflits existants : segments d'un même véhicule dont les fenêtres se chevauchent.
  const conflictIds = useMemo(() => {
    const ids = new Set<string>();
    for (const arr of byVehicle.values()) {
      const act = arr.filter((l) => !TERMINAL.has(l.status));
      for (let i = 0; i < act.length; i++)
        for (let j = i + 1; j < act.length; j++)
          if (legOverlaps(act[i], act[j])) { ids.add(act[i].id); ids.add(act[j].id); }
    }
    return ids;
  }, [byVehicle]);

  // Le glisser-déposer en cours créerait-il un conflit sur ce véhicule ?
  function dropConflict(vehicleId: string): boolean {
    if (!dragId) return false;
    const l = dayLegs.find((x) => x.id === dragId);
    if (!l) return false;
    return (byVehicle.get(vehicleId) ?? [])
      .filter((x) => x.id !== dragId && !TERMINAL.has(x.status))
      .some((x) => legOverlaps(l, x));
  }

  const nowLeft = useMemo(() => {
    const now = Date.now();
    if (now < winStart.getTime() || now > winEnd.getTime()) return null;
    return ((now - winStart.getTime()) / winMs) * gridW;
  }, [winStart, winEnd, winMs, gridW]);

  function geomMs(depMs: number, retMs: number) {
    const s = Math.max(depMs, winStart.getTime());
    const e = Math.min(retMs, winEnd.getTime());
    const left = ((s - winStart.getTime()) / winMs) * gridW;
    const width = Math.max(30, ((e - s) / winMs) * gridW);
    return { left, width };
  }

  function startResize(e: React.MouseEvent, l: LegItem, side: "start" | "end") {
    e.stopPropagation();
    e.preventDefault();
    const desc: ResizeState = { id: l.id, side, startX: e.clientX, origDep: l.depMs, origRet: l.retMs };
    resizeRef.current = desc;
    setResize(desc);
  }

  // Suivi du redimensionnement (souris) : aperçu live + envoi de la replanification au relâché.
  useEffect(() => {
    if (!resize) return;
    const legById = new Map(legs.map((l) => [l.id, l]));
    function onMove(ev: MouseEvent) {
      const d = resizeRef.current;
      if (!d) return;
      // Décalage relatif à l'origine, arrondi au pas de 15 min (zéro déplacement → no-op exact).
      const delta = Math.round((((ev.clientX - d.startX) / gridW) * winMs) / SNAP_MS) * SNAP_MS;
      let dep = d.origDep;
      let ret = d.origRet;
      if (d.side === "start") {
        dep = Math.min(d.origDep + delta, d.origRet - MIN_DUR_MS);
      } else {
        ret = Math.max(d.origRet + delta, d.origDep + MIN_DUR_MS);
      }
      pendingRef.current = { dep, ret };
      setPreview({ id: d.id, dep, ret });
    }
    function onUp() {
      const d = resizeRef.current;
      const p = pendingRef.current;
      resizeRef.current = null;
      pendingRef.current = null;
      setResize(null);
      setPreview(null);
      if (!d || !p) return;
      if (p.dep === d.origDep && p.ret === d.origRet) return; // aucun changement réel
      const item = legById.get(d.id);
      if (!item) return;
      justResized.current = true; // évite la navigation au clic juste après un redimensionnement
      setTimeout(() => { justResized.current = false; }, 0); // libère le drapeau si aucun clic ne suit
      reschedule.mutate(
        { id: item.resId, body: reschedulePayloadForLeg(item, p.dep, p.ret) },
        { onError: (e) => onError?.(apiError(e)) },
      );
    }
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resize, gridW, winMs, legs]);

  function onDropVehicle(vehicleId: string) {
    const id = dragId;
    setDragId(null);
    setOverVeh(null);
    if (!id) return;
    const l = dayLegs.find((x) => x.id === id);
    if (!l || l.vehicle === vehicleId) return;
    assign.mutate({ id, body: { vehicle: vehicleId } }, { onError: (e) => onError?.(apiError(e)) });
  }

  function shift(dir: number) {
    setAnchor((d) => { const x = new Date(d); x.setDate(x.getDate() + dir * (isWeek ? 7 : 1)); return x; });
  }

  function Bar({ l }: { l: LegItem }) {
    const draggable = REASSIGNABLE.has(l.status);
    const resizable = RESCHEDULABLE.has(l.res.status) && l.status === "scheduled";
    const conflicted = conflictIds.has(l.id);
    const previewing = preview?.id === l.id;
    const depMs = previewing ? preview!.dep : l.depMs;
    const retMs = previewing ? preview!.ret : l.retMs;
    const { left, width } = geomMs(depMs, retMs);
    const dep = new Date(depMs);
    const ret = new Date(retMs);
    const legTag = l.isRound ? (l.leg === "outbound" ? "Aller · " : "Retour · ") : "";
    return (
      <div
        draggable={draggable}
        onDragStart={(e) => {
          if (resizeRef.current) { e.preventDefault(); return; } // on redimensionne, pas de déplacement
          e.dataTransfer.effectAllowed = "move";
          setDragId(l.id);
        }}
        onDragEnd={() => { setDragId(null); setOverVeh(null); }}
        onClick={() => {
          if (justResized.current) { justResized.current = false; return; }
          router.push(`/reservations/${l.resId}`);
        }}
        title={`${legTag}${l.destination} · ${dep.toLocaleString("fr-FR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })} → ${fmtTime(ret)}${conflicted ? " · ⚠ chevauchement" : ""}${draggable ? " · glisser pour réaffecter le véhicule de cette course" : ""}${resizable ? " · étirer les bords pour ajuster l'horaire" : ""}`}
        className={cn(
          "absolute top-1 bottom-1 z-10 flex flex-col justify-center overflow-hidden rounded-md px-2 ring-1 ring-inset transition-shadow hover:shadow-md",
          TONE[l.status] ?? "bg-slate-400/15 text-slate-600 ring-slate-400/30",
          conflicted && "ring-2 ring-rose-500/80",
          previewing && "ring-2 ring-brand-500 shadow-lg",
          draggable ? "cursor-grab active:cursor-grabbing" : "cursor-pointer",
          dragId === l.id && "opacity-40",
        )}
        style={{ left, width }}
      >
        <p className="flex items-center gap-1 truncate text-[11px] font-semibold leading-tight">
          {conflicted && <AlertTriangle className="h-3 w-3 shrink-0 text-rose-600" />}
          {l.isRound && <CornerUpLeft className="h-3 w-3 shrink-0" />}
          {legTag}{l.destination}
        </p>
        <p className="truncate text-[10px] leading-tight opacity-80">{fmtTime(dep)}–{fmtTime(ret)}</p>
        {/* Poignées d'étirement — seulement quand le bord est réellement dans la fenêtre
            visible (sinon la poignée serait collée au bord et ne suivrait pas le curseur). */}
        {resizable && depMs >= winStart.getTime() && (
          <span
            onMouseDown={(e) => startResize(e, l, "start")}
            className="absolute inset-y-0 left-0 z-20 w-1.5 cursor-ew-resize rounded-l-md bg-black/10 hover:bg-black/30"
            title="Étirer pour ajuster le départ"
          />
        )}
        {resizable && retMs <= winEnd.getTime() && (
          <span
            onMouseDown={(e) => startResize(e, l, "end")}
            className="absolute inset-y-0 right-0 z-20 w-1.5 cursor-ew-resize rounded-r-md bg-black/10 hover:bg-black/30"
            title="Étirer pour ajuster le retour"
          />
        )}
      </div>
    );
  }

  function Track({ items, vehicleId }: { items: LegItem[]; vehicleId?: string }) {
    const isDropTarget = Boolean(vehicleId);
    const hovered = isDropTarget && overVeh === vehicleId;
    const conflict = hovered && dropConflict(vehicleId!);
    return (
      <div
        className={cn(
          "relative shrink-0",
          hovered && (conflict ? "bg-rose-500/10 ring-2 ring-inset ring-rose-500/60" : "bg-brand-500/10 ring-2 ring-inset ring-brand-500/50"),
        )}
        style={{ width: gridW, height: ROW_H }}
        onDragOver={isDropTarget && dragId ? (e) => { e.preventDefault(); if (overVeh !== vehicleId) setOverVeh(vehicleId!); } : undefined}
        onDrop={isDropTarget ? () => onDropVehicle(vehicleId!) : undefined}
      >
        <div className="pointer-events-none absolute inset-0 flex">
          {Array.from({ length: cols }).map((_, i) => (
            <div key={i} className="border-r border-line/40" style={{ width: colW }} />
          ))}
        </div>
        {conflict && (
          <span className="pointer-events-none absolute right-2 top-1/2 z-20 -translate-y-1/2 rounded-full bg-rose-500 px-2 py-0.5 text-[10px] font-semibold text-white">
            ⚠ conflit horaire
          </span>
        )}
        {nowLeft != null && (
          <div className="pointer-events-none absolute bottom-0 top-0 z-20 w-0.5 bg-rose-500/70" style={{ left: nowLeft }} />
        )}
        {items.map((l) => <Bar key={l.id} l={l} />)}
      </div>
    );
  }

  if (isLoading) return <div className="flex justify-center py-16"><Spinner className="h-7 w-7" /></div>;

  const periodLabel = isWeek
    ? (() => {
        const e = new Date(winStart); e.setDate(e.getDate() + 6);
        return `${winStart.toLocaleDateString("fr-FR", { day: "numeric", month: "short" })} – ${e.toLocaleDateString("fr-FR", { day: "numeric", month: "short", year: "numeric" })}`;
      })()
    : anchor.toLocaleDateString("fr-FR", { weekday: "long", day: "numeric", month: "long", year: "numeric" });

  return (
    <Card>
      <CardBody className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex items-center gap-1">
            <button onClick={() => shift(-1)} className="rounded-md p-1.5 text-muted hover:bg-surface2" aria-label="Précédent"><ChevronLeft className="h-4 w-4" /></button>
            <button onClick={() => setAnchor(startOfDay(new Date()))} className="rounded-md px-2.5 py-1 text-xs font-medium text-muted hover:bg-surface2">Aujourd&apos;hui</button>
            <button onClick={() => shift(1)} className="rounded-md p-1.5 text-muted hover:bg-surface2" aria-label="Suivant"><ChevronRight className="h-4 w-4" /></button>
          </div>
          <p className="text-sm font-semibold capitalize text-ink">{periodLabel}</p>

          {/* Échelle jour / semaine */}
          <div className="ml-2 flex rounded-lg border border-line bg-surface p-0.5">
            {([["day", "Jour"], ["week", "Semaine"]] as const).map(([val, label]) => (
              <button key={val} onClick={() => setRange(val)}
                className={cn("rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
                  range === val ? "bg-brand-600 text-white" : "text-muted hover:bg-surface2")}>
                {label}
              </button>
            ))}
          </div>

          <span className="ml-auto flex items-center gap-1.5 text-[11px] text-faint">
            {assign.isPending && <Spinner className="h-3.5 w-3.5" />}
            Glissez une course d&apos;une ligne à une autre pour réaffecter son véhicule.
          </span>
        </div>

        {vehicles.length === 0 ? (
          <EmptyState title="Aucun véhicule" hint="Ajoutez des véhicules pour visualiser leur occupation." />
        ) : (
          <div className="overflow-x-auto rounded-xl border border-line">
            <div style={{ width: LABEL_W + gridW }}>
              {/* Graduations */}
              <div className="flex border-b border-line bg-surface2/60" style={{ height: 26 }}>
                <div className="sticky left-0 z-30 shrink-0 bg-surface2/60" style={{ width: LABEL_W }} />
                <div className="relative shrink-0" style={{ width: gridW }}>
                  {ticks.map((t, i) => (
                    <span key={i}
                      className={cn("absolute top-1.5 text-[10px] text-faint", isWeek ? "font-medium" : "-translate-x-1/2")}
                      style={{ left: isWeek ? t.left + 6 : t.left }}>
                      {t.label}
                    </span>
                  ))}
                </div>
              </div>

              {/* À affecter */}
              {unassigned.length > 0 && (
                <div className="flex border-b border-line/60 bg-amber-500/5" style={{ height: ROW_H }}>
                  <div className="sticky left-0 z-30 flex shrink-0 items-center gap-2 border-r border-line bg-amber-500/10 px-3" style={{ width: LABEL_W }}>
                    <Inbox className="h-4 w-4 shrink-0 text-amber-600" />
                    <p className="truncate text-xs font-semibold text-amber-700">À affecter ({unassigned.length})</p>
                  </div>
                  <Track items={unassigned} />
                </div>
              )}

              {/* Une ligne par véhicule */}
              {vehicles.map((v) => (
                <div key={v.id} className="flex border-b border-line/60 last:border-0" style={{ height: ROW_H }}>
                  <div className="sticky left-0 z-30 flex shrink-0 items-center gap-2 border-r border-line bg-surface px-3" style={{ width: LABEL_W }}>
                    <Car className="h-4 w-4 shrink-0 text-faint" />
                    <div className="min-w-0">
                      <p className="truncate text-xs font-semibold text-ink">{v.registration}</p>
                      <p className="truncate text-[10px] text-muted">{v.brand} {v.model}</p>
                    </div>
                  </div>
                  <Track items={byVehicle.get(v.id) ?? []} vehicleId={v.id} />
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Légende */}
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-faint">
          <span className="flex items-center gap-1"><span className="h-2.5 w-2.5 rounded-sm bg-rose-500/70 ring-1 ring-rose-500" /> Chevauchement / conflit</span>
          <span className="flex items-center gap-1"><CornerUpLeft className="h-3 w-3" /> Aller-retour (2 courses)</span>
          <span className="flex items-center gap-1"><Inbox className="h-3 w-3" /> À affecter (glisser sur un véhicule)</span>
        </div>
      </CardBody>
    </Card>
  );
}
