"use client";

import { CalendarClock, Gauge, ShieldCheck, TrendingUp, Wrench } from "lucide-react";

import { type Pace, READING_STATE_TONE, type ReadingStatus, type Tracking } from "@/lib/carplan";
import { dueLabel, LEVEL_TONE, type PlanRow, type Reliability } from "@/lib/maintenance";
import { cn, formatNumber } from "@/lib/utils";

import { formatDateTime, formatDay, km, Notice, ToneBadge } from "./shared";

/** Composants de lecture du suivi kilométrique et du calendrier d'entretien — partagés par
 *  « Mon véhicule » (bénéficiaire) et la gestion. Aucun montant : quantités et dates seulement. */

const RELIABILITY_TONE = { good: "green", fair: "amber", low: "red", insufficient: "slate" } as const;

export function LevelBadge({ row }: { row: Pick<PlanRow, "level" | "level_label"> }) {
  return <ToneBadge tone={LEVEL_TONE[row.level] ?? "slate"} label={row.level_label} />;
}

export function ReadingStateBadge({ reading }: { reading: ReadingStatus }) {
  return <ToneBadge tone={READING_STATE_TONE[reading.state] ?? "slate"} label={reading.state_label} />;
}

export function ReliabilityBadge({ reliability }: { reliability: Reliability }) {
  const label = reliability.score != null ? `${reliability.label} · ${reliability.score}/100` : reliability.label;
  return <ToneBadge tone={RELIABILITY_TONE[reliability.level] ?? "slate"} label={label} />;
}

export function paceText(pace: Pace | null | undefined): string {
  if (!pace || pace.km_per_day == null) return "Données insuffisantes";
  return `${formatNumber(pace.km_per_day)} km/jour`;
}

function Tile({ icon: Icon, label, value, sub, tone }: {
  icon: React.ElementType; label: string; value: React.ReactNode; sub?: React.ReactNode; tone?: "warn" | "danger";
}) {
  return (
    <div className={cn("flex items-start gap-3 rounded-xl border px-3 py-3",
      tone === "danger" ? "border-rose-500/40 bg-rose-500/5" : tone === "warn" ? "border-amber-500/40 bg-amber-500/5" : "border-line")}>
      <Icon className={cn("mt-0.5 h-4 w-4 shrink-0", tone === "danger" ? "text-rose-600" : tone === "warn" ? "text-amber-600" : "text-muted")} />
      <div className="min-w-0">
        <p className="text-[11px] text-faint">{label}</p>
        <div className="text-sm font-semibold text-ink">{value}</div>
        {sub && <div className="text-[11px] text-muted">{sub}</div>}
      </div>
    </div>
  );
}

/** Synthèse : compteur, dernière déclaration, prochain relevé attendu, rythme, prochaine
 *  opération (distance restante, date prévisionnelle), fiabilité des données. */
export function TrackingOverview({ tracking: t }: { tracking: Tracking }) {
  const r = t.reading;
  const next = t.next_operation;
  const late = r.state === "late" || r.state === "stale";
  return (
    <div className="space-y-3">
      {r.required && late && (
        <Notice tone="warning">
          {r.state === "stale" ? "Votre dernier relevé est trop ancien" : "Relevé kilométrique en retard"} : déclarez votre
          compteur pour garder des prévisions d&apos;entretien fiables.
        </Notice>
      )}
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        <Tile icon={Gauge} label="Compteur actuel" value={t.current_odometer != null ? km(t.current_odometer) : "—"}
              sub={r.last_reading ? <>Dernière déclaration : {formatDateTime(r.last_reading.recorded_at)} · {km(r.last_reading.odometer)}</>
                : "Aucune déclaration"} />
        <Tile icon={CalendarClock} label="Prochain relevé attendu"
              value={r.required ? formatDay(r.next_due) : "Non exigé"}
              sub={r.required ? <span className="inline-flex flex-wrap items-center gap-1.5"><ReadingStateBadge reading={r} />
                <span>tous les {r.frequency_days} jours</span></span> : undefined}
              tone={r.state === "stale" || r.state === "late" ? "danger" : r.state === "due" ? "warn" : undefined} />
        <Tile icon={TrendingUp} label="Moyenne kilométrique" value={paceText(t.pace)}
              sub={t.pace ? <>{t.pace.label}{t.pace.pace_increase ? " · rythme en nette hausse" : ""}</> : undefined} />
        <Tile icon={Wrench} label={next ? `Prochaine opération : ${next.operation}` : "Prochaine opération"}
              value={next?.expected_date ? <>{formatDay(next.expected_date)} <span className="font-normal text-muted">({dueLabel(next)})</span></>
                : "Aucune échéance connue"}
              sub={next ? <span className="inline-flex flex-wrap items-center gap-1.5"><LevelBadge row={next} />
                {next.remaining_km != null && <span>{next.remaining_km > 0 ? `${km(next.remaining_km)} restants` : "Seuil km atteint"}</span>}
                {next.preliminary && <span>· estimation préliminaire</span>}</span> : undefined}
              tone={next && (next.level === "urgent" || next.level === "overdue") ? "danger" : next?.level === "alert" ? "warn" : undefined} />
      </div>
      <div className="flex flex-wrap items-center gap-2 rounded-xl border border-line px-3 py-2.5">
        <ShieldCheck className="h-4 w-4 text-muted" />
        <span className="text-xs text-muted">Fiabilité des données</span>
        <ReliabilityBadge reliability={t.reliability} />
        {t.reliability.factors.length > 0 && (
          <ul className="w-full list-disc pl-6 text-[11px] text-muted">
            {t.reliability.factors.map((f) => <li key={f}>{f}</li>)}
          </ul>
        )}
      </div>
    </div>
  );
}

/** Calendrier des entretiens : une carte par opération (mobile d'abord), la plus proche en tête. */
export function MaintenanceCalendar({ rows, showVehicle = false }: { rows: PlanRow[]; showVehicle?: boolean }) {
  if (!rows.length) return <p className="text-sm text-muted">Aucune opération suivie pour ce véhicule.</p>;
  return (
    <ul className="space-y-2">
      {rows.map((r) => (
        <li key={`${r.source}-${r.id ?? r.kind}-${r.vehicle}`}
            className={cn("rounded-xl border px-3 py-2.5",
              r.level === "overdue" || r.level === "urgent" ? "border-rose-500/40 bg-rose-500/5"
                : r.level === "alert" ? "border-amber-500/40 bg-amber-500/5" : "border-line")}>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-sm font-semibold text-ink">
              {showVehicle && <span className="mr-1 font-mono text-xs text-muted">{r.registration}</span>}{r.operation}
            </p>
            <LevelBadge row={r} />
          </div>
          <div className="mt-1 grid grid-cols-2 gap-x-3 gap-y-1 text-[11px] text-muted sm:grid-cols-4">
            <span>Échéance : <span className="font-medium text-ink">{r.expected_date ? formatDay(r.expected_date) : "inconnue"}</span>
              {r.expected_date && <> · {dueLabel(r)}</>}</span>
            <span>{r.trigger === "km" ? "Limite km atteinte en premier" : r.trigger === "date" ? "Limite calendaire en premier" : "—"}</span>
            {r.due_mileage != null && (
              <span>Seuil : <span className="font-medium text-ink">{km(r.due_mileage)}</span>
                {r.remaining_km != null && <> · {r.remaining_km > 0 ? `${km(r.remaining_km)} restants` : "atteint"}</>}</span>
            )}
            {r.last_done_date || r.last_done_mileage != null ? (
              <span>Dernier : {r.last_done_date ? formatDay(r.last_done_date) : "—"}
                {r.last_done_mileage != null && r.last_done_mileage >= 0 ? ` · ${km(r.last_done_mileage)}` : ""}</span>
            ) : r.source === "plan" ? <span>Dernier entretien non renseigné</span> : null}
          </div>
          {(r.preliminary || (r.forecast_label && !r.forecast_km_date)) && (
            <p className="mt-1 text-[10px] text-faint">{r.preliminary ? "Estimation préliminaire (deux relevés seulement)" : r.forecast_label}</p>
          )}
        </li>
      ))}
    </ul>
  );
}

export function PaceSummary({ pace }: { pace: Pace | null }) {
  return (
    <span className="inline-flex items-center gap-1 text-xs text-muted">
      <Gauge className="h-3.5 w-3.5" /> {paceText(pace)}
      {pace?.method === "preliminary" && <span className="text-faint">(préliminaire)</span>}
    </span>
  );
}
