"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { AlertTriangle, Car, ChevronLeft, ChevronRight, CornerUpLeft, Inbox } from "lucide-react";

import { Card, CardBody, EmptyState, Spinner } from "@/components/ui";
import { apiError } from "@/lib/api";
import { useReservationAction, useVehicles } from "@/lib/queries";
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

// Statuts pour lesquels (ré)affecter un véhicule est possible (cf. services.assign_vehicle).
const REASSIGNABLE = new Set(["approved", "vehicle_assigned", "driver_assigned"]);
// Ignorés : n'occupent pas un véhicule.
const HIDDEN = new Set(["draft", "rejected", "cancelled"]);
// Pris en compte pour la détection de conflits (occupation réelle du véhicule).
const TERMINAL = new Set(["completed", "closed"]);
// Statuts dont les horaires peuvent encore être replanifiés (cf. RESCHEDULABLE_STATUSES backend).
const RESCHEDULABLE = new Set([
  "submitted", "pending_manager", "pending_fleet", "approved", "vehicle_assigned", "driver_assigned",
]);
const SNAP_MS = 15 * 60 * 1000; // pas de 15 min
const MIN_DUR_MS = 30 * 60 * 1000; // durée minimale d'une course

const TONE: Record<string, string> = {
  approved: "bg-violet-500/15 text-violet-700 ring-violet-500/30",
  vehicle_assigned: "bg-sky-500/15 text-sky-700 ring-sky-500/30",
  driver_assigned: "bg-indigo-500/15 text-indigo-700 ring-indigo-500/30",
  in_progress: "bg-emerald-500/15 text-emerald-700 ring-emerald-500/30",
  returned: "bg-amber-500/15 text-amber-700 ring-amber-500/30",
  completed: "bg-slate-400/20 text-slate-600 ring-slate-400/30",
  closed: "bg-slate-400/20 text-slate-600 ring-slate-400/30",
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
function overlaps(a: Reservation, b: Reservation) {
  return ms(a.departure_time) < ms(b.estimated_return) && ms(b.departure_time) < ms(a.estimated_return);
}

/**
 * Planning d'occupation des véhicules : une ligne par véhicule, le temps en abscisse
 * (jour ou semaine). Glisser-déposer d'une commande d'une ligne à une autre pour
 * (ré)affecter le véhicule ; les chevauchements sont surlignés en rouge et signalés
 * avant le dépôt (la validation finale des conflits reste côté serveur).
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
  const assign = useReservationAction("assign-vehicle");
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

  // Réservations croisant la fenêtre.
  const dayRes = useMemo(
    () => reservations.filter((r) => {
      if (HIDDEN.has(r.status)) return false;
      return ms(r.estimated_return) > winStart.getTime() && ms(r.departure_time) < winEnd.getTime();
    }),
    [reservations, winStart, winEnd],
  );

  const byVehicle = useMemo(() => {
    const m = new Map<string, Reservation[]>();
    for (const r of dayRes) {
      if (!r.vehicle) continue;
      const arr = m.get(r.vehicle) ?? [];
      arr.push(r);
      m.set(r.vehicle, arr);
    }
    return m;
  }, [dayRes]);

  const unassigned = useMemo(() => dayRes.filter((r) => !r.vehicle && REASSIGNABLE.has(r.status)), [dayRes]);

  // Conflits existants : barres d'un même véhicule dont les fenêtres se chevauchent.
  const conflictIds = useMemo(() => {
    const ids = new Set<string>();
    for (const arr of byVehicle.values()) {
      const act = arr.filter((r) => !TERMINAL.has(r.status));
      for (let i = 0; i < act.length; i++)
        for (let j = i + 1; j < act.length; j++)
          if (overlaps(act[i], act[j])) { ids.add(act[i].id); ids.add(act[j].id); }
    }
    return ids;
  }, [byVehicle]);

  // Le glisser-déposer en cours créerait-il un conflit sur ce véhicule ?
  function dropConflict(vehicleId: string): boolean {
    if (!dragId) return false;
    const r = dayRes.find((x) => x.id === dragId);
    if (!r) return false;
    return (byVehicle.get(vehicleId) ?? [])
      .filter((x) => x.id !== dragId && !TERMINAL.has(x.status))
      .some((x) => overlaps(r, x));
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

  function startResize(e: React.MouseEvent, r: Reservation, side: "start" | "end") {
    e.stopPropagation();
    e.preventDefault();
    const desc: ResizeState = { id: r.id, side, startX: e.clientX, origDep: ms(r.departure_time), origRet: ms(r.estimated_return) };
    resizeRef.current = desc;
    setResize(desc);
  }

  // Suivi du redimensionnement (souris) : aperçu live + envoi de la replanification au relâché.
  useEffect(() => {
    if (!resize) return;
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
      justResized.current = true; // évite la navigation au clic juste après un redimensionnement
      setTimeout(() => { justResized.current = false; }, 0); // libère le drapeau si aucun clic ne suit
      reschedule.mutate(
        { id: d.id, body: { departure_time: new Date(p.dep).toISOString(), estimated_return: new Date(p.ret).toISOString() } },
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
  }, [resize, gridW, winMs]);

  function onDropVehicle(vehicleId: string) {
    const id = dragId;
    setDragId(null);
    setOverVeh(null);
    if (!id) return;
    const r = dayRes.find((x) => x.id === id);
    if (!r || r.vehicle === vehicleId) return;
    assign.mutate({ id, body: { vehicle: vehicleId } }, { onError: (e) => onError?.(apiError(e)) });
  }

  function shift(dir: number) {
    setAnchor((d) => { const x = new Date(d); x.setDate(x.getDate() + dir * (isWeek ? 7 : 1)); return x; });
  }

  function Bar({ r }: { r: Reservation }) {
    const draggable = REASSIGNABLE.has(r.status);
    const resizable = RESCHEDULABLE.has(r.status);
    const conflicted = conflictIds.has(r.id);
    const previewing = preview?.id === r.id;
    const depMs = previewing ? preview!.dep : ms(r.departure_time);
    const retMs = previewing ? preview!.ret : ms(r.estimated_return);
    const { left, width } = geomMs(depMs, retMs);
    const dep = new Date(depMs);
    const ret = new Date(retMs);
    return (
      <div
        draggable={draggable}
        onDragStart={(e) => {
          if (resizeRef.current) { e.preventDefault(); return; } // on redimensionne, pas de déplacement
          e.dataTransfer.effectAllowed = "move";
          setDragId(r.id);
        }}
        onDragEnd={() => { setDragId(null); setOverVeh(null); }}
        onClick={() => {
          if (justResized.current) { justResized.current = false; return; }
          router.push(`/reservations/${r.id}`);
        }}
        title={`${r.destination} · ${dep.toLocaleString("fr-FR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })} → ${fmtTime(ret)}${conflicted ? " · ⚠ chevauchement" : ""}${draggable ? " · glisser pour réaffecter" : ""}${resizable ? " · étirer les bords pour ajuster l'horaire" : ""}`}
        className={cn(
          "absolute top-1 bottom-1 z-10 flex flex-col justify-center overflow-hidden rounded-md px-2 ring-1 ring-inset transition-shadow hover:shadow-md",
          TONE[r.status] ?? "bg-slate-400/15 text-slate-600 ring-slate-400/30",
          conflicted && "ring-2 ring-rose-500/80",
          previewing && "ring-2 ring-brand-500 shadow-lg",
          draggable ? "cursor-grab active:cursor-grabbing" : "cursor-pointer",
          dragId === r.id && "opacity-40",
        )}
        style={{ left, width }}
      >
        <p className="flex items-center gap-1 truncate text-[11px] font-semibold leading-tight">
          {conflicted && <AlertTriangle className="h-3 w-3 shrink-0 text-rose-600" />}
          {r.trip_type === "round_trip" && <CornerUpLeft className="h-3 w-3 shrink-0" />}
          {r.destination}
        </p>
        <p className="truncate text-[10px] leading-tight opacity-80">{fmtTime(dep)}–{fmtTime(ret)}</p>
        {/* Poignées d'étirement — seulement quand le bord est réellement dans la fenêtre
            visible (sinon la poignée serait collée au bord et ne suivrait pas le curseur). */}
        {resizable && depMs >= winStart.getTime() && (
          <span
            onMouseDown={(e) => startResize(e, r, "start")}
            className="absolute inset-y-0 left-0 z-20 w-1.5 cursor-ew-resize rounded-l-md bg-black/10 hover:bg-black/30"
            title="Étirer pour ajuster le départ"
          />
        )}
        {resizable && retMs <= winEnd.getTime() && (
          <span
            onMouseDown={(e) => startResize(e, r, "end")}
            className="absolute inset-y-0 right-0 z-20 w-1.5 cursor-ew-resize rounded-r-md bg-black/10 hover:bg-black/30"
            title="Étirer pour ajuster le retour"
          />
        )}
      </div>
    );
  }

  function Track({ items, vehicleId }: { items: Reservation[]; vehicleId?: string }) {
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
        {items.map((r) => <Bar key={r.id} r={r} />)}
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
            Glissez une commande d&apos;une ligne à une autre pour réaffecter le véhicule.
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
          <span className="flex items-center gap-1"><CornerUpLeft className="h-3 w-3" /> Aller-retour</span>
          <span className="flex items-center gap-1"><Inbox className="h-3 w-3" /> À affecter (glisser sur un véhicule)</span>
        </div>
      </CardBody>
    </Card>
  );
}
