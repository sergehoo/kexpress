"use client";

import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import { AlertTriangle, CalendarClock, Gauge, Plus, Siren, Wrench } from "lucide-react";

import { LevelBadge, ReliabilityBadge } from "@/components/carplan/MileageTracking";
import { FormError, formatDateTime, formatDay, km, Notice, Pager } from "@/components/carplan/shared";
import { Modal } from "@/components/Modal";
import { StatChips } from "@/components/StatChips";
import { Tabs } from "@/components/Tabs";
import { Button, Card, CardBody, EmptyState, Input, Label, Select, Spinner } from "@/components/ui";
import { apiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import {
  dueLabel, LEVEL_LABEL, PLAN_EVENT_LABEL, type PlanBody, type PlanLevel, type PlanRow, useCreatePlan,
  useMaintenanceOutlook, useMaintenancePlan, useMaintenancePlans, useMaintenanceTypeRefs, usePlanEvents, useUpdatePlan,
} from "@/lib/maintenance";
import { useVehicles } from "@/lib/queries";
import { formatNumber } from "@/lib/utils";

const READERS = new Set(["super_admin", "company_admin", "subsidiary_admin", "fleet_manager", "finance", "auditor"]);
const WRITERS = new Set(["super_admin", "company_admin", "subsidiary_admin", "fleet_manager"]);
const PAGE_SIZE = 50;

/** Page maintenance : interventions (existant) + plans d'entretien prédictifs. `?plan=<id>`
 *  (lien des notifications) ouvre directement le plan. */
export function MaintenanceSections({ records }: { records: React.ReactNode }) {
  const { me } = useAuth();
  const params = useSearchParams();
  const plan = params.get("plan");
  const canRead = !!me && READERS.has(me.role);
  if (!canRead) return <>{records}</>;
  return (
    <Tabs initialKey={plan ? "plans" : "records"} items={[
      { key: "records", label: "Interventions", content: records },
      { key: "plans", label: "Plans d'entretien prédictifs", content: <PlansPanel initialPlan={plan} /> },
    ]} />
  );
}

function PlansPanel({ initialPlan }: { initialPlan: string | null }) {
  const { me } = useAuth();
  const canWrite = !!me && WRITERS.has(me.role);
  const outlook = useMaintenanceOutlook();
  const [level, setLevel] = useState("");
  const [page, setPage] = useState(1);
  const plans = useMaintenancePlans({ ...(level ? { level } : {}), page: String(page), page_size: String(PAGE_SIZE) });
  const [editing, setEditing] = useState<string | null>(initialPlan);
  const [creating, setCreating] = useState(false);
  useEffect(() => { if (initialPlan) setEditing(initialPlan); }, [initialPlan]);
  const o = outlook.data;

  return (
    <div className="space-y-5">
      {outlook.isLoading ? <div className="flex justify-center py-8"><Spinner /></div> : !o ? (
        <Notice tone="danger">{apiError(outlook.error, "Prévisions indisponibles.")}</Notice>
      ) : (
        <>
          <StatChips stats={[
            { label: "Dépassés", value: o.counts.overdue, icon: Siren, tone: "bg-rose-500/10 text-rose-600" },
            { label: "Urgents (≤ 3 j)", value: o.counts.urgent, icon: AlertTriangle, tone: "bg-rose-500/10 text-rose-600" },
            { label: "Alertes (≤ 7 j)", value: o.counts.alert, icon: CalendarClock, tone: "bg-amber-500/10 text-amber-600" },
            { label: "Préavis (≤ 14 j)", value: o.counts.notice, icon: Wrench, tone: "bg-sky-500/10 text-sky-600" },
            { label: "Plans suivis", value: o.plans, icon: Gauge, tone: "bg-brand-500/10 text-brand-600",
              sub: o.counts.unknown ? `${o.counts.unknown} à initialiser` : undefined },
          ]} />

          <Card>
            <CardBody className="space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3 className="text-sm font-semibold text-ink">Entretiens à venir, urgents ou dépassés</h3>
                {canWrite && <Button size="sm" onClick={() => setCreating(true)}><Plus className="h-4 w-4" /> Nouveau plan</Button>}
              </div>
              {o.watch.length === 0 ? <EmptyState title="Aucune échéance proche" hint="Aucun plan en préavis, alerte, urgence ou dépassement." />
                : <PlanTable rows={o.watch} onEdit={setEditing} />}
              {o.watch_count > o.watch.length && (
                <p className="text-[11px] text-faint">{o.watch.length} échéances les plus pressantes sur {o.watch_count} : les autres figurent dans « Tous les plans » (filtre par niveau).</p>
              )}
            </CardBody>
          </Card>
        </>
      )}

      <Card>
        <CardBody className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-sm font-semibold text-ink">Tous les plans</h3>
            <Select aria-label="Niveau" value={level} onChange={(e) => { setLevel(e.target.value); setPage(1); }} className="sm:w-48">
              <option value="">Tous les niveaux</option>
              {(Object.keys(LEVEL_LABEL) as PlanLevel[]).map((l) => <option key={l} value={l}>{LEVEL_LABEL[l]}</option>)}
            </Select>
          </div>
          {plans.isLoading ? <Spinner /> : !(plans.data?.results ?? []).length ? <EmptyState title="Aucun plan" />
            : <PlanTable rows={plans.data?.results ?? []} onEdit={setEditing} />}
          {plans.data && <Pager count={plans.data.count} page={plans.data.page} pageSize={plans.data.page_size} onPage={setPage} />}
        </CardBody>
      </Card>

      {o && (
        <div className="grid gap-5 lg:grid-cols-2">
          <Card>
            <CardBody className="space-y-2">
              <h3 className="text-sm font-semibold text-ink">Fiabilité des prévisions</h3>
              {o.forecasts_count > o.forecasts.length && <p className="text-[11px] text-faint">{o.forecasts.length} véhicules affichés sur {o.forecasts_count}.</p>}
              {o.forecasts.length === 0 ? <p className="text-xs text-muted">Aucun véhicule suivi.</p> : (
                <ul className="divide-y divide-line text-sm">
                  {o.forecasts.map((f) => (
                    <li key={f.vehicle} className="flex flex-wrap items-center justify-between gap-2 py-2">
                      <span className="font-mono text-xs font-semibold text-ink">{f.registration}</span>
                      <span className="text-xs text-muted">{f.km_per_day != null ? `${formatNumber(f.km_per_day)} km/jour` : "Données insuffisantes"}
                        {f.label && f.km_per_day != null ? ` · ${f.label}` : ""}</span>
                      {f.reliability ? <ReliabilityBadge reliability={f.reliability} /> : <span className="text-[11px] text-faint">hors Car Plan</span>}
                    </li>
                  ))}
                </ul>
              )}
            </CardBody>
          </Card>
          <Card>
            <CardBody className="space-y-2">
              <h3 className="text-sm font-semibold text-ink">Historique des alertes et des interventions</h3>
              {o.events.length === 0 && o.interventions.length === 0 ? <p className="text-xs text-muted">Aucun événement.</p> : (
                <ul className="max-h-96 divide-y divide-line overflow-y-auto text-xs">
                  {o.events.map((e) => (
                    <li key={`e-${e.id}`} className="py-2">
                      <p className="flex flex-wrap items-center gap-1.5 text-ink">
                        <span className="font-medium">{PLAN_EVENT_LABEL[e.kind] ?? e.kind_label}</span>
                        <span className="font-mono text-muted">{e.registration}</span> · {e.operation}
                        {e.level && e.level !== "ok" && <LevelBadge row={{ level: e.level, level_label: e.level_label ?? e.level }} />}
                      </p>
                      <p className="text-muted">{e.message}</p>
                      <p className="text-faint">{formatDateTime(e.at)}{e.recipients ? ` · ${e.recipients} destinataire(s)` : ""}{e.actor ? ` · ${e.actor}` : ""}</p>
                    </li>
                  ))}
                  {o.interventions.map((r) => (
                    <li key={`r-${r.id}`} className="py-2">
                      <p className="text-ink"><span className="font-medium">Intervention terminée</span> · <span className="font-mono text-muted">{r.registration}</span> · {r.operation}</p>
                      <p className="text-faint">{formatDay(r.performed_date)}{r.mileage != null ? ` · ${km(r.mileage)}` : ""}</p>
                    </li>
                  ))}
                </ul>
              )}
            </CardBody>
          </Card>
        </div>
      )}

      {editing && <PlanDialog id={editing} canWrite={canWrite} onClose={() => setEditing(null)} />}
      {creating && <NewPlanDialog onClose={() => setCreating(false)} onCreated={(id) => { setCreating(false); setEditing(id); }} />}
    </div>
  );
}

function PlanTable({ rows, onEdit }: { rows: PlanRow[]; onEdit: (id: string) => void }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead><tr className="border-b border-line text-left text-[11px] uppercase tracking-wide text-faint">
          <th className="px-3 py-2 font-medium">Véhicule</th><th className="px-3 py-2 font-medium">Opération</th>
          <th className="px-3 py-2 font-medium">Échéance</th><th className="px-3 py-2 text-right font-medium">Reste</th>
          <th className="px-3 py-2 font-medium">Prévision</th><th className="px-3 py-2 font-medium">Niveau</th>
        </tr></thead>
        <tbody className="divide-y divide-line">
          {rows.map((r) => (
            <tr key={r.id ?? `${r.vehicle}-${r.kind}`} className="cursor-pointer hover:bg-surface2" onClick={() => r.id && onEdit(r.id)}>
              <td className="whitespace-nowrap px-3 py-2 font-mono text-xs font-semibold text-ink">{r.registration}</td>
              <td className="px-3 py-2 text-ink">{r.operation}</td>
              <td className="whitespace-nowrap px-3 py-2 text-muted">
                {r.expected_date ? formatDay(r.expected_date) : "Inconnue"}
                <span className="block text-[11px] text-faint">{r.expected_date ? dueLabel(r) : ""}
                  {r.trigger ? ` · limite ${r.trigger === "km" ? "km" : "calendaire"}` : ""}</span>
              </td>
              <td className="whitespace-nowrap px-3 py-2 text-right text-muted">
                {r.remaining_km != null ? (r.remaining_km > 0 ? km(r.remaining_km) : "Atteint") : "—"}
                {r.due_mileage != null && <span className="block text-[11px] text-faint">seuil {km(r.due_mileage)}</span>}
              </td>
              <td className="px-3 py-2 text-[11px] text-muted">
                {r.km_per_day != null ? `${formatNumber(r.km_per_day)} km/j` : "—"}
                {r.forecast_label && <span className="block text-faint">{r.preliminary ? "Estimation préliminaire" : r.forecast_label}</span>}
              </td>
              <td className="px-3 py-2"><LevelBadge row={r} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function intOrNull(v: string): number | null {
  if (v.trim() === "") return null;
  const n = Number(v);
  return Number.isInteger(n) && n >= 0 ? n : NaN;
}

function PlanDialog({ id, canWrite, onClose }: { id: string; canWrite: boolean; onClose: () => void }) {
  const { data, isLoading, error: loadError } = useMaintenancePlan(id);
  const events = usePlanEvents(id);
  const update = useUpdatePlan();
  const s = data?.settings;
  const [form, setForm] = useState<Record<string, string>>({});
  const [active, setActive] = useState(true);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!s) return;
    setForm({
      last_done_date: s.last_done_date ?? "", last_done_mileage: s.last_done_mileage != null ? String(s.last_done_mileage) : "",
      interval_km: s.interval_km != null ? String(s.interval_km) : "", interval_days: s.interval_days != null ? String(s.interval_days) : "",
      due_date: s.due_date ?? "", due_mileage: s.due_mileage != null && s.last_done_mileage == null ? String(s.due_mileage) : "",
    });
    setActive(s.is_active);
  }, [s]);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (!s) return;
    // Seuls les champs MODIFIÉS partent : un km de dernier entretien déjà reporté sur un compteur
    // remplacé (valeur éventuellement négative) n'est jamais renvoyé ni reporté une seconde fois.
    const body: PlanBody = {};
    const text = (v: number | string | null) => (v == null ? "" : String(v));
    if ((form.last_done_date ?? "") !== text(s.last_done_date)) body.last_done_date = form.last_done_date || null;
    if ((form.due_date ?? "") !== text(s.due_date)) body.due_date = form.due_date || null;
    if (active !== s.is_active) body.is_active = active;
    if ((form.last_done_mileage ?? "") !== text(s.last_done_mileage)) body.last_done_mileage = intOrNull(form.last_done_mileage ?? "");
    if ((form.interval_km ?? "") !== text(s.interval_km)) body.interval_km = intOrNull(form.interval_km ?? "");
    if ((form.interval_days ?? "") !== text(s.interval_days)) body.interval_days = intOrNull(form.interval_days ?? "");
    const manualDue = s.due_mileage != null && s.last_done_mileage == null ? String(s.due_mileage) : "";
    if ((form.due_mileage ?? "") !== manualDue && (form.due_mileage ?? "") !== "") body.due_mileage = intOrNull(form.due_mileage);
    if (Object.values(body).some((v) => typeof v === "number" && Number.isNaN(v))) { setError("Valeurs entières positives attendues."); return; }
    if (Object.keys(body).length === 0) { onClose(); return; }
    update.mutate({ id, body }, { onSuccess: onClose, onError: (err) => setError(apiError(err)) });
  }

  const field = (name: string, label: string, type = "number", hint?: string) => (
    <div><Label htmlFor={`plan-${name}`}>{label}</Label>
      <Input id={`plan-${name}`} type={type} min={type === "number" ? 0 : undefined} value={form[name] ?? ""} disabled={!canWrite}
             onChange={(e) => setForm({ ...form, [name]: e.target.value })} />
      {hint && <p className="mt-0.5 text-[10px] text-faint">{hint}</p>}</div>
  );

  return (
    <Modal open title={data ? `${data.operation} — ${data.registration}` : "Plan d'entretien"} onClose={onClose} className="max-h-[94vh] max-w-2xl overflow-y-auto">
      {isLoading ? <div className="flex justify-center py-8"><Spinner /></div> : !data ? (
        <Notice tone="danger">{apiError(loadError, "Plan introuvable ou hors de votre périmètre.")}</Notice>
      ) : (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
            <LevelBadge row={data} />
            <span>Échéance {data.expected_date ? `${formatDay(data.expected_date)} (${dueLabel(data)})` : "inconnue"}</span>
            {data.remaining_km != null && <span>· {data.remaining_km > 0 ? `${km(data.remaining_km)} restants` : "seuil km atteint"}</span>}
            {data.km_per_day != null && <span>· {formatNumber(data.km_per_day)} km/jour ({data.preliminary ? "estimation préliminaire" : data.forecast_label})</span>}
          </div>
          <form onSubmit={submit} noValidate className="space-y-3">
            <div className="grid gap-3 sm:grid-cols-2">
              {field("last_done_date", "Dernier entretien — date", "date")}
              {field("last_done_mileage", "Dernier entretien — km", "number", "Km lu au compteur ce jour-là : reporté automatiquement si le compteur a été remplacé depuis.")}
              {field("interval_km", "Périodicité km (dérogation)", "number", "Vide : celle de l'opération.")}
              {field("interval_days", "Périodicité jours (dérogation)", "number", "Vide : celle de l'opération.")}
              {field("due_date", "Date limite imposée", "date", "Facultative : s'ajoute à l'échéance calendaire.")}
              {field("due_mileage", "Seuil km manuel", "number", "Seulement si le dernier entretien est inconnu.")}
            </div>
            <label className="flex items-center gap-2 text-sm text-ink">
              <input type="checkbox" checked={active} disabled={!canWrite} onChange={(e) => setActive(e.target.checked)} className="h-4 w-4 accent-brand-600" />
              Plan actif
            </label>
            <p className="text-[11px] text-faint">Le dernier entretien se met à jour automatiquement lorsqu&apos;une intervention de ce type passe à « Terminée ».</p>
            <FormError message={error} />
            {canWrite && (
              <div className="flex justify-end gap-2">
                <Button type="button" variant="secondary" onClick={onClose}>Fermer</Button>
                <Button type="submit" disabled={update.isPending}>{update.isPending && <Spinner className="h-4 w-4" />} Enregistrer</Button>
              </div>
            )}
          </form>
          <div>
            <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-faint">Historique du plan</p>
            {events.isLoading ? <Spinner /> : !(events.data ?? []).length ? <p className="text-xs text-muted">Aucun événement.</p> : (
              <ul className="divide-y divide-line text-xs">
                {(events.data ?? []).map((e) => (
                  <li key={e.id} className="py-1.5">
                    <span className="font-medium text-ink">{PLAN_EVENT_LABEL[e.kind] ?? e.kind_label}</span>
                    <span className="text-faint"> · {formatDateTime(e.at)}{e.recipients ? ` · ${e.recipients} destinataire(s)` : ""}{e.actor ? ` · ${e.actor}` : ""}</span>
                    {e.message && <p className="text-muted">{e.message}</p>}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </Modal>
  );
}

function NewPlanDialog({ onClose, onCreated }: { onClose: () => void; onCreated: (id: string) => void }) {
  const { data: vehicles } = useVehicles({ page_size: "200" });
  const { data: types } = useMaintenanceTypeRefs();
  const create = useCreatePlan();
  const [vehicle, setVehicle] = useState("");
  const [type, setType] = useState("");
  const [lastDate, setLastDate] = useState("");
  const [lastKm, setLastKm] = useState("");
  const [error, setError] = useState("");
  const vehicleRows = useMemo(() => vehicles?.results ?? [], [vehicles]);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (!vehicle || !type) { setError("Véhicule et opération obligatoires."); return; }
    const kmValue = intOrNull(lastKm);
    if (kmValue !== null && Number.isNaN(kmValue)) { setError("Kilométrage entier positif."); return; }
    create.mutate({ vehicle, maintenance_type: type, last_done_date: lastDate || null, last_done_mileage: kmValue }, {
      onSuccess: (row) => onCreated(row.settings.id), onError: (err) => setError(apiError(err)),
    });
  }

  return (
    <Modal open title="Nouveau plan d'entretien" onClose={onClose}>
      <form onSubmit={submit} className="space-y-3">
        <div><Label htmlFor="np-vehicle">Véhicule</Label>
          <Select id="np-vehicle" value={vehicle} onChange={(e) => setVehicle(e.target.value)} required>
            <option value="">Choisir</option>
            {vehicleRows.map((v) => <option key={v.id} value={v.id}>{v.registration} — {v.brand} {v.model}</option>)}
          </Select></div>
        <div><Label htmlFor="np-type">Opération</Label>
          <Select id="np-type" value={type} onChange={(e) => setType(e.target.value)} required>
            <option value="">Choisir</option>
            {(types ?? []).map((t) => (
              <option key={t.id} value={t.id}>{t.name}{t.interval_km ? ` · ${formatNumber(t.interval_km)} km` : ""}{t.interval_days ? ` · ${t.interval_days} j` : ""}</option>
            ))}
          </Select>
          <p className="mt-0.5 text-[10px] text-faint">Les opérations se configurent (périodicités, seuils) dans le référentiel des types de maintenance.</p></div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div><Label htmlFor="np-date">Dernier entretien — date</Label>
            <Input id="np-date" type="date" value={lastDate} onChange={(e) => setLastDate(e.target.value)} /></div>
          <div><Label htmlFor="np-km">Dernier entretien — km</Label>
            <Input id="np-km" type="number" min={0} value={lastKm} onChange={(e) => setLastKm(e.target.value)} /></div>
        </div>
        <FormError message={error} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose}>Annuler</Button>
          <Button type="submit" disabled={create.isPending}>{create.isPending && <Spinner className="h-4 w-4" />} Créer</Button>
        </div>
      </form>
    </Modal>
  );
}
