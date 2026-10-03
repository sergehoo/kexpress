"use client";

import { useState } from "react";

import { EmptyState, Select, Spinner } from "@/components/ui";
import { carPlanError, useMileageFollowup, type FollowupFilter, type FollowupRow } from "@/lib/carplan";
import { dueLabel } from "@/lib/maintenance";

import { LevelBadge, paceText, ReadingStateBadge, ReliabilityBadge } from "./MileageTracking";
import { formatDateTime, formatDay, km, Notice, Pager } from "./shared";

const FILTERS: { value: FollowupFilter; label: string }[] = [
  { value: "", label: "Toutes les attributions en cours" },
  { value: "late", label: "Relevés en retard ou trop anciens" },
  { value: "maintenance", label: "Entretien urgent ou dépassé" },
  { value: "unreliable", label: "Prévisions peu fiables" },
];
const PAGE_SIZE = 40;

/** Gestion : relevés attendus, en retard ou trop anciens, fiabilité des prévisions et entretien
 *  le plus proche — attributions en cours du périmètre. Filtre, compteurs et pagination sont
 *  calculés par l'API. Aucun montant. */
export function MileageFollowupPanel({ onOpen }: { onOpen: (id: string) => void }) {
  const [filter, setFilter] = useState<FollowupFilter>("");
  const [page, setPage] = useState(1);
  const { data, isLoading, error } = useMileageFollowup({ state: filter, page, pageSize: PAGE_SIZE });
  const rows = data?.results ?? [];
  const counts = data?.counts;

  if (isLoading) return <div className="flex justify-center py-10"><Spinner /></div>;
  if (error) return <Notice tone="danger">{carPlanError(error)}</Notice>;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <Select aria-label="Filtre" value={filter} className="sm:w-72"
                onChange={(e) => { setFilter(e.target.value as FollowupFilter); setPage(1); }}>
          {FILTERS.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
        </Select>
        {counts && <span className="text-xs text-muted">{counts.total} attribution(s) en cours · {counts.late} relevé(s) en retard · {counts.maintenance} entretien(s) urgent(s) ou dépassé(s)</span>}
      </div>
      {rows.length === 0 ? <EmptyState title="Rien à signaler" hint="Aucune attribution en cours ne correspond à ce filtre." /> : (
        <ul className="grid gap-2 lg:grid-cols-2">
          {rows.map((r) => <FollowupCard key={r.assignment} row={r} onOpen={onOpen} />)}
        </ul>
      )}
      {data && <Pager count={data.count} page={data.page} pageSize={data.page_size} onPage={setPage} />}
    </div>
  );
}

function FollowupCard({ row: r, onOpen }: { row: FollowupRow; onOpen: (id: string) => void }) {
  const next = r.next_operation;
  return (
    <li>
      <button type="button" onClick={() => onOpen(r.assignment)}
              className="w-full rounded-xl border border-line bg-surface p-3 text-left transition hover:border-brand-500/40 hover:bg-surface2">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0">
            <p className="font-mono text-sm font-semibold text-ink">{r.registration}</p>
            <p className="truncate text-xs text-muted">{r.beneficiary_name} · {r.reference} · {r.subsidiary_name}</p>
          </div>
          <ReadingStateBadge reading={r.reading} />
        </div>
        <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 text-[11px] text-muted">
          <div><dt className="text-faint">Dernier relevé</dt>
            <dd className="text-ink">{r.reading.last_reading ? `${km(r.reading.last_reading.odometer)} · ${formatDateTime(r.reading.last_reading.recorded_at)}` : "—"}</dd></div>
          <div><dt className="text-faint">Relevé attendu</dt>
            <dd className="text-ink">{r.reading.required ? `${formatDay(r.reading.next_due)}${r.reading.late_days ? ` (retard ${r.reading.late_days} j)` : ""}` : "Non exigé"}</dd></div>
          <div><dt className="text-faint">Moyenne</dt><dd className="text-ink">{paceText(r.pace)}</dd></div>
          <div><dt className="text-faint">Fiabilité</dt><dd><ReliabilityBadge reliability={r.reliability} /></dd></div>
          <div className="col-span-2"><dt className="text-faint">Prochaine opération</dt>
            <dd className="flex flex-wrap items-center gap-1.5 text-ink">
              {next ? <>{next.operation} · {next.expected_date ? `${formatDay(next.expected_date)} (${dueLabel(next)})` : "date inconnue"}
                <LevelBadge row={next} /></> : "Aucune échéance connue"}
            </dd></div>
          {r.last_reminder && <div className="col-span-2 text-faint">{r.last_reminder.label} le {formatDateTime(r.last_reminder.at)}</div>}
        </dl>
      </button>
    </li>
  );
}
