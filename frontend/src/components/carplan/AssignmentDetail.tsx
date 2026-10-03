"use client";

import { useState } from "react";
import { AlertTriangle, Plus, Square } from "lucide-react";

import { Modal } from "@/components/Modal";
import { Tabs } from "@/components/Tabs";
import { Button, EmptyState, Input, Label, Select, Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth";
import {
  ASSIGNMENT_STATUS_LABEL, canCarPlan, carPlanError, EVENT_LABEL, httpStatus, MILEAGE_DECLARATION_LABEL,
  REPLACEMENT_STATUS_TONE, useAssignment, useAssignmentComparison, useAssignmentContributions, useAssignmentCosts,
  useAssignmentEvents, useAssignmentInspections, useAssignmentReplacements, useAssignmentUsage,
  useCarPlanVehicles, useEndReplacement, useManagerMileage, useRecordContribution, useStartReplacement,
  type Assignment, type AssignmentEvent, type AssignmentStatus,
} from "@/lib/carplan";
import { cn, formatNumber } from "@/lib/utils";

import { AssignmentActions } from "./AssignmentActions";
import { AssignmentTrackingPanel, ManagerReadingsTable } from "./MileageManagement";
import { ComparisonPanel, InspectionCard, InspectionForm } from "./inspections";
import {
  AssignmentStatusBadge, decimalOrNull, Drawer, type Flash, FormError, formatDateTime, formatDay, formatMonth,
  GaugeBar, InfoRow, km, money, Notice, SectionTitle, Textarea, todayISO, ToneBadge,
} from "./shared";

const IN_USE: AssignmentStatus[] = ["active", "suspended", "returning"];

/** Panneau de détail d'une attribution (gestion). Ouvert depuis la liste, le tableau de bord,
 *  une demande / un incident, ou le lien d'une notification (`/car-plan?assignment=<id>`). */
export function AssignmentDetail({ id, onClose, onOpen }: {
  id: string;
  onClose: () => void;
  /** Ouvre une autre attribution (renouvellement créé, attribution d'origine). */
  onOpen: (id: string) => void;
}) {
  const { me } = useAuth();
  const { data: a, isLoading, error } = useAssignment(id);
  const [flash, setFlash] = useState<Flash>(null);
  const [inspectionKind, setInspectionKind] = useState<"handover" | "return" | null>(null);
  const canCosts = canCarPlan(me, "view_carplan_costs");

  const notify = (text: string, tone: "success" | "danger" = "success") => setFlash({ tone, text });

  return (
    <Drawer
      open
      onClose={onClose}
      title={a ? <span className="flex flex-wrap items-center gap-2">{a.reference} <AssignmentStatusBadge status={a.status} label={a.status_display} /></span> : "Attribution"}
      subtitle={a ? `${a.beneficiary_name} · ${a.assignment_type_display}${a.vehicle_registration ? ` · ${a.vehicle_registration}` : ""}` : undefined}
    >
      {isLoading ? (
        <div className="flex justify-center py-16"><Spinner className="h-7 w-7" /></div>
      ) : !a ? (
        <EmptyState title={httpStatus(error) === 404 ? "Attribution introuvable ou hors de votre périmètre" : "Attribution indisponible"}
                    hint={error ? carPlanError(error) : undefined} />
      ) : (
        <div className="space-y-4">
          {flash && (
            <Notice tone={flash.tone}>
              <span className="flex items-start justify-between gap-2">{flash.text}
                <button className="text-[11px] underline" onClick={() => setFlash(null)}>Masquer</button></span>
            </Notice>
          )}
          {a.attention && <Notice tone="warning">{a.attention}</Notice>}
          <Tabs
            items={[
              { key: "info", label: "Informations", content: (
                <InfoTab a={a} onOpen={onOpen} onInspection={setInspectionKind}
                         onDone={(msg, created) => { notify(msg); if (created?.id && created.id !== a.id) onOpen(created.id); }} />
              ) },
              { key: "inspections", label: "États des lieux", content: (
                <InspectionsTab a={a} onFlash={notify} onInspection={setInspectionKind} />
              ) },
              { key: "usage", label: "Relevés, entretien & quotas", content: <UsageTab a={a} onFlash={notify} /> },
              { key: "replacements", label: "Remplacements", content: <ReplacementsTab a={a} onFlash={notify} /> },
              { key: "history", label: "Historique", content: <HistoryTab id={a.id} /> },
              ...(canCosts ? [{ key: "costs", label: "Coûts & participations", content: <CostsTab a={a} onFlash={notify} /> }] : []),
            ]}
          />
        </div>
      )}
      {a && inspectionKind && (
        <InspectionFormLoader a={a} kind={inspectionKind} onClose={() => setInspectionKind(null)}
                              onCreated={() => { setInspectionKind(null); notify("État des lieux enregistré : le bénéficiaire doit maintenant le valider depuis son espace."); }} />
      )}
    </Drawer>
  );
}

/** Préremplit l'état des lieux de restitution depuis la remise validée du véhicule courant. */
function InspectionFormLoader({ a, kind, onClose, onCreated }: {
  a: Assignment; kind: "handover" | "return"; onClose: () => void; onCreated: () => void;
}) {
  const { data, isLoading } = useAssignmentInspections(a.id);
  const usage = useAssignmentUsage(a.id);
  if (isLoading || usage.isLoading) return null;
  const handover = (data ?? []).filter((i) => i.kind === "handover" && i.vehicle === a.vehicle && i.manager_signed_at)
    .sort((x, y) => y.performed_at.localeCompare(x.performed_at))[0] ?? null;
  const lastOdometer = usage.data?.last_reading?.odometer ?? null;
  const defaultMileage = kind === "return" ? (lastOdometer ?? handover?.mileage ?? a.start_mileage) : lastOdometer;
  return (
    <InspectionForm assignmentId={a.id} kind={kind} defaultMileage={defaultMileage}
                    template={kind === "return" ? handover : null} onClose={onClose} onCreated={onCreated} />
  );
}

// --- Informations + circuit ----------------------------------------------------------------

function InfoTab({ a, onOpen, onDone, onInspection }: {
  a: Assignment;
  onOpen: (id: string) => void;
  onDone: (message: string, created?: { id: string; reference?: string }) => void;
  onInspection: (kind: "handover" | "return") => void;
}) {
  const quota = (v: number | string | null | undefined, unit: string) => (v === null || v === undefined ? "—" : formatNumber(v, unit));
  return (
    <div className="space-y-5">
      <section className="rounded-xl border border-line bg-surface p-4">
        <SectionTitle>Actions</SectionTitle>
        <AssignmentActions assignment={a} onDone={onDone} onInspection={onInspection} />
      </section>

      <section className="rounded-xl border border-line bg-surface p-4">
        <SectionTitle>Attribution</SectionTitle>
        <dl className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <InfoRow label="Bénéficiaire">{a.beneficiary_name}<span className="block text-xs text-muted">{a.beneficiary_email}</span></InfoRow>
          <InfoRow label="Filiale d'imputation">{a.subsidiary_name}</InfoRow>
          <InfoRow label="Service">{a.department_name ?? "—"}</InfoRow>
          <InfoRow label="Centre de coût">{a.cost_center_label ?? "—"}</InfoRow>
          <InfoRow label="Type">{a.assignment_type_display}</InfoRow>
          <InfoRow label="Politique">{a.policy_name} <span className="text-xs text-muted">(version {a.policy_version_number})</span></InfoRow>
          <InfoRow label="Véhicule">{a.vehicle_registration ? `${a.vehicle_registration} — ${a.vehicle_label ?? ""}` : "Non attribué"}</InfoRow>
          <InfoRow label="Début">{formatDay(a.start_date)}</InfoRow>
          <InfoRow label="Fin prévue">{a.planned_end_date ? formatDay(a.planned_end_date) : "Sans fin prévue"}</InfoRow>
          <InfoRow label="Restitution réelle">{formatDay(a.actual_return_date)}</InfoRow>
          <InfoRow label="Kilométrage initial / final">{a.start_mileage != null ? km(a.start_mileage) : "—"} / {a.end_mileage != null ? km(a.end_mileage) : "—"}</InfoRow>
          <InfoRow label="Demandée par">{a.requested_by_name}<span className="block text-xs text-muted">{formatDateTime(a.created_at)}</span></InfoRow>
          <InfoRow label="Approuvée par">{a.approved_by_name ?? "—"}{a.approved_at && <span className="block text-xs text-muted">{formatDateTime(a.approved_at)}</span>}</InfoRow>
          {a.renewal_of && (
            <InfoRow label="Renouvellement de">
              <button className="text-brand-600 hover:underline" onClick={() => onOpen(a.renewal_of!)}>Voir l&apos;attribution d&apos;origine</button>
            </InfoRow>
          )}
        </dl>
      </section>

      <section className="rounded-xl border border-line bg-surface p-4">
        <SectionTitle>Quotas</SectionTitle>
        <dl className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          <InfoRow label="Km mensuel">{quota(a.monthly_km_quota, "km")}</InfoRow>
          <InfoRow label="Km annuel">{quota(a.annual_km_quota, "km")}</InfoRow>
          <InfoRow label="Carburant mensuel">{quota(a.monthly_fuel_liters_quota, "L")}</InfoRow>
          <InfoRow label="Recharge mensuelle">{quota(a.monthly_energy_kwh_quota, "kWh")}</InfoRow>
        </dl>
        {a.special_conditions && (
          <div className="mt-4">
            <p className="text-[11px] font-medium uppercase tracking-wide text-faint">Conditions particulières</p>
            <p className="mt-1 whitespace-pre-line text-sm text-ink">{a.special_conditions}</p>
          </div>
        )}
      </section>
    </div>
  );
}

// --- États des lieux --------------------------------------------------------------------------

function InspectionsTab({ a, onFlash, onInspection }: {
  a: Assignment; onFlash: (text: string, tone?: "success" | "danger") => void; onInspection: (kind: "handover" | "return") => void;
}) {
  const { me } = useAuth();
  const canManage = canCarPlan(me, "manage_carplan_assignments");
  const { data, isLoading } = useAssignmentInspections(a.id);
  const list = data ?? [];
  const hasReturn = list.some((i) => i.kind === "return");
  const comparison = useAssignmentComparison(a.id, hasReturn);
  const kind: "handover" | "return" | null = a.status === "allocated" ? "handover" : IN_USE.includes(a.status) ? "return" : null;
  const pendingSameKind = kind && list.some((i) => i.kind === kind && i.vehicle === a.vehicle && !i.manager_signed_at);

  return (
    <div className="space-y-3">
      {canManage && kind && (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-xs text-muted">
            {kind === "handover" ? "La remise active l'attribution une fois validée par les deux parties."
              : "La restitution libère le véhicule une fois validée par les deux parties."}
          </p>
          <Button size="sm" disabled={!!pendingSameKind} onClick={() => onInspection(kind)}
                  title={pendingSameKind ? "Un état des lieux de cette nature est déjà en cours." : undefined}>
            <Plus className="h-4 w-4" /> {kind === "handover" ? "État des lieux de remise" : "État des lieux de restitution"}
          </Button>
        </div>
      )}
      {isLoading ? <div className="flex justify-center py-10"><Spinner /></div>
        : list.length === 0 ? <EmptyState title="Aucun état des lieux" hint={kind === "handover" ? "Réalisez l'état des lieux de remise du véhicule." : undefined} />
        : list.map((i, idx) => (
          <InspectionCard key={i.id} inspection={i} as="manager" canSign={canManage} canAddPhoto={canManage}
                          defaultOpen={idx === 0 && !i.is_signed} onFlash={onFlash} />
        ))}
      {hasReturn && <ComparisonPanel comparison={comparison.data} />}
    </div>
  );
}

// --- Consommation, quotas, relevés --------------------------------------------------------------

function UsageTab({ a, onFlash }: { a: Assignment; onFlash: (text: string, tone?: "success" | "danger") => void }) {
  const { me } = useAuth();
  const canManage = canCarPlan(me, "manage_carplan_assignments");
  const usage = useAssignmentUsage(a.id);
  const declare = useManagerMileage();
  const [odometer, setOdometer] = useState("");
  const [date, setDate] = useState(todayISO());
  const [error, setError] = useState("");
  const u = usage.data;

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    const n = Number(odometer);
    if (!odometer.trim() || !Number.isInteger(n) || n < 0) { setError("Compteur en km (nombre entier)."); return; }
    declare.mutate({ assignmentId: a.id, odometer: n, reading_date: date || undefined }, {
      onSuccess: () => { setOdometer(""); onFlash("Relevé enregistré."); },
      onError: (err) => setError(carPlanError(err)),
    });
  }

  return (
    <div className="space-y-4">
      <AssignmentTrackingPanel a={a} canManage={canManage} onFlash={onFlash} />
      <section className="rounded-xl border border-line bg-surface p-4">
        <SectionTitle>Quotas {u ? `· ${formatMonth(u.month)}` : ""}</SectionTitle>
        {usage.isLoading ? <Spinner /> : !u ? <p className="text-xs text-muted">Consommation indisponible.</p> : (
          <div className="space-y-3">
            <GaugeBar label="Kilométrage du mois" gauge={u.km_month} unit="km" />
            <GaugeBar label="Kilométrage de l'année" gauge={u.km_year} unit="km" />
            <GaugeBar label="Carburant du mois" gauge={u.fuel_liters_month} unit="L" />
            <GaugeBar label="Recharge du mois" gauge={u.energy_kwh_month} unit="kWh" />
            <dl className="grid grid-cols-2 gap-3 border-t border-line pt-3 sm:grid-cols-4">
              <InfoRow label="Déclaration">{MILEAGE_DECLARATION_LABEL[u.declaration] ?? u.declaration}</InfoRow>
              <InfoRow label="Km professionnels (mois)">{u.professional_km_month != null ? km(u.professional_km_month) : "—"}</InfoRow>
              <InfoRow label="Km privés (mois)">{u.private_km_month != null ? km(u.private_km_month) : "—"}</InfoRow>
              <InfoRow label="Dernier relevé">
                {u.last_reading ? <>{km(u.last_reading.odometer)}<span className="block text-xs text-muted">{formatDay(u.last_reading.date)} · {u.last_reading.source}</span></> : "—"}
              </InfoRow>
            </dl>
          </div>
        )}
      </section>

      {canManage && IN_USE.includes(a.status) && (
        <form onSubmit={submit} className="rounded-xl border border-line bg-surface p-4">
          <SectionTitle>Relevé gestionnaire</SectionTitle>
          <div className="grid gap-3 sm:grid-cols-[1fr_1fr_auto] sm:items-end">
            <div><Label htmlFor="mgr-odo">Compteur (km)</Label>
              <Input id="mgr-odo" type="number" min={0} inputMode="numeric" value={odometer} onChange={(e) => setOdometer(e.target.value)} /></div>
            <div><Label htmlFor="mgr-date">Date du relevé</Label>
              <Input id="mgr-date" type="date" max={todayISO()} value={date} onChange={(e) => setDate(e.target.value)} /></div>
            <Button type="submit" disabled={declare.isPending}>{declare.isPending && <Spinner className="h-4 w-4" />} Enregistrer</Button>
          </div>
          <div className="mt-2"><FormError message={error} /></div>
        </form>
      )}

      <ManagerReadingsTable a={a} canManage={canManage} onFlash={onFlash} />
    </div>
  );
}

// --- Véhicules de remplacement -----------------------------------------------------------------

function ReplacementsTab({ a, onFlash }: { a: Assignment; onFlash: (text: string, tone?: "success" | "danger") => void }) {
  const { me } = useAuth();
  const canManage = canCarPlan(me, "manage_carplan_assignments");
  const { data, isLoading } = useAssignmentReplacements(a.id);
  const [creating, setCreating] = useState(false);
  const [ending, setEnding] = useState<string | null>(null);
  const list = data ?? [];
  const hasCurrent = list.some((r) => r.status === "planned" || r.status === "active");
  const canStart = canManage && (a.status === "active" || a.status === "suspended") && !hasCurrent;

  return (
    <div className="space-y-3">
      {canStart && (
        <div className="flex justify-end">
          <Button size="sm" onClick={() => setCreating(true)}><Plus className="h-4 w-4" /> Véhicule de remplacement</Button>
        </div>
      )}
      {isLoading ? <div className="flex justify-center py-10"><Spinner /></div>
        : list.length === 0 ? <EmptyState title="Aucun véhicule de remplacement" hint="Immobilisation, entretien : un véhicule de la flotte mutualisée prend le relais, 3 mois au plus." />
        : (
          <ul className="space-y-2">
            {list.map((r) => (
              <li key={r.id} className="flex flex-wrap items-center gap-3 rounded-xl border border-line bg-surface px-4 py-3">
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium text-ink">{r.vehicle_registration} — {r.vehicle_label}</p>
                  <p className="text-xs text-muted">
                    Du {formatDay(r.start_date)} au {formatDay(r.end_date)}{r.actual_end_date ? ` · rendu le ${formatDay(r.actual_end_date)}` : ""}
                  </p>
                  {r.reason && <p className="mt-0.5 text-xs text-muted">{r.reason}</p>}
                </div>
                <ToneBadge tone={REPLACEMENT_STATUS_TONE[r.status] ?? "slate"} label={r.status_display} />
                {canManage && (r.status === "planned" || r.status === "active") && (
                  <Button size="sm" variant="secondary" onClick={() => setEnding(r.id)}><Square className="h-3.5 w-3.5" /> Terminer</Button>
                )}
              </li>
            ))}
          </ul>
        )}
      {creating && <ReplacementForm a={a} onClose={() => setCreating(false)} onDone={() => { setCreating(false); onFlash("Véhicule de remplacement attribué : le bénéficiaire est prévenu."); }} />}
      {ending && <EndReplacementDialog id={ending} onClose={() => setEnding(null)} onDone={() => { setEnding(null); onFlash("Remplacement terminé."); }} />}
    </div>
  );
}

function ReplacementForm({ a, onClose, onDone }: { a: Assignment; onClose: () => void; onDone: () => void }) {
  const vehicles = useCarPlanVehicles("pool");
  const start = useStartReplacement();
  const [vehicle, setVehicle] = useState("");
  const [startDate, setStartDate] = useState(todayISO());
  const [endDate, setEndDate] = useState("");
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const options = (vehicles.data ?? []).filter((v) => v.id !== a.vehicle && !v.holder);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (!vehicle || !startDate || !endDate || !reason.trim()) { setError("Véhicule, période et motif sont obligatoires."); return; }
    start.mutate({ assignmentId: a.id, body: { vehicle, start_date: startDate, end_date: endDate, reason: reason.trim() } }, {
      onSuccess: onDone,
      onError: (err) => setError(carPlanError(err)),
    });
  }

  return (
    <Modal open title="Véhicule de remplacement" onClose={onClose} className="max-w-lg">
      <form onSubmit={submit} className="space-y-3">
        <Notice tone="info">Le véhicule est pris dans la flotte mutualisée et soustrait au dispatching sur la période. L&apos;attribution principale demeure.</Notice>
        <div><Label htmlFor="rep-vehicle">Véhicule (flotte mutualisée)</Label>
          <Select id="rep-vehicle" value={vehicle} onChange={(e) => setVehicle(e.target.value)} required>
            <option value="">{vehicles.isLoading ? "Chargement…" : "Choisir un véhicule"}</option>
            {options.map((v) => <option key={v.id} value={v.id}>{v.registration} — {v.label}{v.subsidiary_name ? ` (${v.subsidiary_name})` : ""}</option>)}
          </Select></div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div><Label htmlFor="rep-start">Du</Label><Input id="rep-start" type="date" value={startDate} required onChange={(e) => setStartDate(e.target.value)} /></div>
          <div><Label htmlFor="rep-end">Au (prévu)</Label><Input id="rep-end" type="date" value={endDate} min={startDate} required onChange={(e) => setEndDate(e.target.value)} /></div>
        </div>
        <div><Label htmlFor="rep-reason">Motif</Label><Textarea id="rep-reason" value={reason} required onChange={(e) => setReason(e.target.value)} /></div>
        <FormError message={error} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose}>Annuler</Button>
          <Button type="submit" disabled={start.isPending}>{start.isPending && <Spinner className="h-4 w-4" />} Attribuer</Button>
        </div>
      </form>
    </Modal>
  );
}

function EndReplacementDialog({ id, onClose, onDone }: { id: string; onClose: () => void; onDone: () => void }) {
  const end = useEndReplacement();
  const [onDate, setOnDate] = useState(todayISO());
  const [error, setError] = useState("");
  return (
    <Modal open title="Terminer le remplacement" onClose={onClose}>
      <div className="space-y-3">
        <div><Label htmlFor="rep-on">Rendu le</Label><Input id="rep-on" type="date" value={onDate} onChange={(e) => setOnDate(e.target.value)} /></div>
        <p className="text-xs text-muted">Un remplacement prévu qui n&apos;a pas commencé est annulé.</p>
        <FormError message={error} />
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>Annuler</Button>
          <Button disabled={end.isPending} onClick={() => end.mutate({ id, on_date: onDate || undefined }, {
            onSuccess: onDone, onError: (err) => setError(carPlanError(err)),
          })}>{end.isPending && <Spinner className="h-4 w-4" />} Terminer</Button>
        </div>
      </div>
    </Modal>
  );
}

// --- Historique -------------------------------------------------------------------------------

const DETAIL_LABEL: Record<string, string> = {
  previous_end: "ancienne fin", new_end: "nouvelle fin", mileage: "kilométrage", odometer: "compteur",
  reading_date: "relevé du", starts_at: "du", ends_at: "au", renewal: "renouvellement", continuity_of: "suite de",
  on_date: "à compter du", start: "du", end: "au", party: "partie", drivable: "véhicule roulant", reason: "motif",
  previous: "valeur erronée", old_odometer: "ancien compteur", new_odometer: "nouveau compteur", days: "fréquence (jours)",
  anomaly: "atypique", by: "par",
};

/** Clés d'alerte automatique (anti-doublon de l'historique) → libellé. */
const ALERT_LABEL: Record<string, string> = {
  expiring: "Échéance de l'attribution", late: "Restitution en retard", quota: "Quota", handover: "Remise imminente",
  approval: "Validation en attente", compliance: "Véhicule non conforme", reading_due: "Rappel de relevé kilométrique",
  reading_relaunch: "Relance de relevé kilométrique", reading_late: "Relevé en retard signalé aux gestionnaires",
};

function detailValue(key: string, value: unknown): string {
  if (key === "party" || key === "by") return value === "beneficiary" ? "bénéficiaire" : "gestionnaire";
  if (key === "drivable") return value ? "oui" : "non";
  if (key === "starts_at" || key === "ends_at") return formatDateTime(String(value));
  if (typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value)) return formatDay(value);
  if (typeof value === "number") return formatNumber(value);
  return String(value);
}

function HistoryTab({ id }: { id: string }) {
  const { data, isLoading } = useAssignmentEvents(id);
  if (isLoading) return <div className="flex justify-center py-10"><Spinner /></div>;
  const events = [...(data ?? [])].reverse();
  if (events.length === 0) return <EmptyState title="Aucun événement" />;
  return (
    <ol className="relative space-y-3 border-l border-line pl-5">
      {events.map((e: AssignmentEvent) => {
        const details = Object.entries(e.details ?? {}).filter(([k, v]) => DETAIL_LABEL[k] && v !== "" && v !== null && v !== undefined);
        const isAlert = e.kind === "alert";
        return (
          <li key={e.id} className="relative">
            <span className={cn("absolute -left-[26px] top-1 h-3 w-3 rounded-full border-2 border-surface",
              isAlert ? "bg-amber-500" : e.to_status ? "bg-brand-500" : "bg-slate-400")} />
            <div className="flex flex-wrap items-baseline gap-x-2">
              <span className="text-sm font-medium text-ink">{EVENT_LABEL[e.kind] ?? e.kind}</span>
              {e.from_status && e.to_status && (
                <span className="text-[11px] text-muted">
                  {ASSIGNMENT_STATUS_LABEL[e.from_status as AssignmentStatus] ?? e.from_status} → {ASSIGNMENT_STATUS_LABEL[e.to_status as AssignmentStatus] ?? e.to_status}
                </span>
              )}
            </div>
            <p className="text-[11px] text-faint">{formatDateTime(e.at)} · {e.actor_name}</p>
            {e.note && <p className="mt-0.5 whitespace-pre-line text-xs text-muted">{e.note}</p>}
            {isAlert && typeof e.details?.key === "string" && (
              <p className="mt-0.5 flex items-center gap-1 text-[11px] text-amber-700 dark:text-amber-300">
                <AlertTriangle className="h-3 w-3" /> {ALERT_LABEL[String(e.details.key).split(":")[0]] ?? String(e.details.key).split(":")[0]}
              </p>
            )}
            {details.length > 0 && (
              <p className="mt-0.5 text-[11px] text-muted">
                {details.map(([k, v]) => `${DETAIL_LABEL[k]} : ${detailValue(k, v)}`).join(" · ")}
              </p>
            )}
          </li>
        );
      })}
    </ol>
  );
}

// --- Coûts & participations (profils coûts) -----------------------------------------------------

function CostsTab({ a, onFlash }: { a: Assignment; onFlash: (text: string, tone?: "success" | "danger") => void }) {
  const { me } = useAuth();
  const canContribute = canCarPlan(me, "manage_carplan_contributions");
  const costs = useAssignmentCosts(a.id);
  const contributions = useAssignmentContributions(a.id);
  const record = useRecordContribution();
  const [period, setPeriod] = useState(todayISO().slice(0, 7));
  const [amount, setAmount] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const c = costs.data;

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    const value = decimalOrNull(amount);
    if (!period || !value) { setError("Mois et montant obligatoires."); return; }
    record.mutate({ assignmentId: a.id, body: { period: `${period}-01`, amount: value, note: note.trim() || undefined } }, {
      onSuccess: () => { setAmount(""); setNote(""); onFlash("Participation enregistrée (à part du coût, qu'elle ne réduit pas)."); },
      onError: (err) => setError(carPlanError(err)),
    });
  }

  return (
    <div className="space-y-4">
      {costs.isLoading ? <div className="flex justify-center py-10"><Spinner /></div>
        : costs.error ? <Notice tone="danger">{carPlanError(costs.error)}</Notice>
        : c && (
          <>
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
              {[
                { label: "Coût total de détention", value: money(c.total) },
                { label: "Kilomètres", value: km(c.km) },
                { label: "Coût / km", value: c.cost_per_km != null ? money(c.cost_per_km) : "—" },
                { label: "Participations du bénéficiaire", value: money(c.employee_contributions) },
              ].map((k) => (
                <div key={k.label} className="rounded-xl border border-line bg-surface px-3 py-2.5">
                  <p className="text-base font-semibold text-ink">{k.value}</p>
                  <p className="text-[11px] text-muted">{k.label}</p>
                </div>
              ))}
            </div>
            {c.provisional && <Notice tone="warning">Coût provisoire : au moins un mois n&apos;est pas encore clos.</Notice>}
            <p className="text-[11px] text-faint">
              Part du coût réel de chaque véhicule détenu, au prorata des jours ; la part absorbée par des courses mutualisées
              (mise à disposition) en est retirée. Les participations du bénéficiaire sont rapportées à côté : elles ne réduisent pas le coût.
            </p>
            <div className="overflow-x-auto rounded-xl border border-line bg-surface">
              <table className="w-full text-sm">
                <thead><tr className="border-b border-line text-left text-[11px] uppercase tracking-wide text-faint">
                  <th className="px-3 py-2 font-medium">Mois</th><th className="px-3 py-2 font-medium">Véhicule(s)</th>
                  <th className="px-3 py-2 text-right font-medium">Énergie</th><th className="px-3 py-2 text-right font-medium">Fixes</th>
                  <th className="px-3 py-2 text-right font-medium">Autres</th><th className="px-3 py-2 text-right font-medium">Absorbé</th>
                  <th className="px-3 py-2 text-right font-medium">Total</th><th className="px-3 py-2 text-right font-medium">Km</th>
                  <th className="px-3 py-2 text-right font-medium">Coût/km</th>
                </tr></thead>
                <tbody className="divide-y divide-line">
                  {c.months.length === 0 ? (
                    <tr><td colSpan={9} className="px-3 py-6 text-center text-xs text-muted">Aucun mois écoulé.</td></tr>
                  ) : [...c.months].reverse().map((m) => (
                    <tr key={m.period}>
                      <td className="whitespace-nowrap px-3 py-2 text-ink">{formatMonth(m.period)}{m.provisional && <span className="ml-1 text-[10px] text-amber-600">provisoire</span>}</td>
                      <td className="px-3 py-2 text-xs text-muted">{m.vehicles.map((v) => `${v.registration} (${v.days} j${v.kind === "replacement" ? ", rempl." : ""})`).join(", ") || "—"}</td>
                      <td className="whitespace-nowrap px-3 py-2 text-right text-muted">{m.energy != null ? formatNumber(m.energy) : "—"}</td>
                      <td className="whitespace-nowrap px-3 py-2 text-right text-muted">{m.fixed != null ? formatNumber(m.fixed) : "—"}</td>
                      <td className="whitespace-nowrap px-3 py-2 text-right text-muted">{m.other != null ? formatNumber(m.other) : "—"}</td>
                      <td className="whitespace-nowrap px-3 py-2 text-right text-muted">{m.absorbed_by_pool_trips != null ? formatNumber(m.absorbed_by_pool_trips) : "—"}</td>
                      <td className="whitespace-nowrap px-3 py-2 text-right font-medium text-ink">{m.total != null ? formatNumber(m.total) : "non valorisé"}</td>
                      <td className="whitespace-nowrap px-3 py-2 text-right text-muted">{formatNumber(m.km)}</td>
                      <td className="whitespace-nowrap px-3 py-2 text-right text-muted">{m.cost_per_km != null ? formatNumber(m.cost_per_km) : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="text-[11px] text-faint">Montants en XOF.</p>
          </>
        )}

      <section className="rounded-xl border border-line bg-surface p-4">
        <SectionTitle>Participations du bénéficiaire</SectionTitle>
        {contributions.isLoading ? <Spinner /> : !(contributions.data ?? []).length ? (
          <p className="text-xs text-muted">Aucune participation enregistrée.</p>
        ) : (
          <ul className="divide-y divide-line text-sm">
            {(contributions.data ?? []).map((p) => (
              <li key={p.id} className="flex flex-wrap items-center gap-2 py-2">
                <span className="w-32 text-ink">{formatMonth(p.period)}</span>
                <span className="font-medium text-ink">{money(p.amount)}</span>
                <span className="flex-1 text-xs text-muted">{p.note}</span>
                <span className="text-[11px] text-faint">{p.recorded_by} · {formatDateTime(p.created_at)}</span>
              </li>
            ))}
          </ul>
        )}
        {canContribute && (
          <form onSubmit={submit} className="mt-3 grid gap-2 border-t border-line pt-3 sm:grid-cols-[10rem_10rem_1fr_auto] sm:items-end">
            <div><Label htmlFor="ctb-period">Mois</Label><Input id="ctb-period" type="month" value={period} onChange={(e) => setPeriod(e.target.value)} /></div>
            <div><Label htmlFor="ctb-amount">Montant (XOF)</Label><Input id="ctb-amount" inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} placeholder="0" /></div>
            <div><Label htmlFor="ctb-note">Note</Label><Input id="ctb-note" value={note} onChange={(e) => setNote(e.target.value)} /></div>
            <Button type="submit" disabled={record.isPending}>{record.isPending && <Spinner className="h-4 w-4" />} Enregistrer</Button>
            <div className="sm:col-span-4"><FormError message={error} /></div>
          </form>
        )}
      </section>
    </div>
  );
}

