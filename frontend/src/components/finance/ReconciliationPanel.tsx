"use client";

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, ArrowRight, CheckCircle2, Info, Search } from "lucide-react";

import { Button, Card, CardBody, EmptyState, Input, Label, Select, Spinner } from "@/components/ui";
import { Modal } from "@/components/Modal";
import { money } from "@/components/finance/RealCostPanel";
import { api, apiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { CATEGORY_LABEL, useLegacyExpenses, useReconcile, type LegacyExpense } from "@/lib/financeF2";
import { useFinancePeriods, useMaintenanceTypes } from "@/lib/queries";
import { canFinance } from "@/lib/rbac";
import { formatDate, formatNumber } from "@/lib/utils";

type Destination =
  | "attach" | "fuel_log" | "electric_charge" | "maintenance" | "insurance" | "vehicle_charge" | "generic";

/** Destinations de reprise (apps/finance/reconciliation.py). `effect` dit OÙ vivra le coût :
 *  c'est ce qui garantit qu'il n'est compté qu'une fois. */
const DESTINATIONS: { value: Destination; label: string; effect: string; needsVehicle: boolean }[] = [
  { value: "attach", label: "Rattacher à un enregistrement existant", needsVehicle: false,
    effect: "La dépense devient la pièce de l'enregistrement choisi : aucun montant ne s'ajoute, le coût reste porté par cet enregistrement." },
  { value: "fuel_log", label: "Plein carburant", needsVehicle: true,
    effect: "Un plein de carburant est créé avec ce montant et cette date ; la dépense en devient la pièce." },
  { value: "electric_charge", label: "Recharge électrique", needsVehicle: true,
    effect: "Une recharge électrique est créée avec ce montant et cette date ; la dépense en devient la pièce." },
  { value: "maintenance", label: "Maintenance", needsVehicle: true,
    effect: "Une maintenance réalisée est créée avec ce coût et cette date ; la dépense en devient la pièce." },
  { value: "insurance", label: "Assurance", needsVehicle: true,
    effect: "Une police d'assurance est créée avec ce coût ; la dépense en devient la pièce." },
  { value: "vehicle_charge", label: "Autre charge spécialisée (pneus, abonnement, taxe…)", needsVehicle: true,
    effect: "Une charge véhicule est créée sur la période indiquée ; la dépense en devient la pièce." },
  { value: "generic", label: "Dépense générique réelle", needsVehicle: false,
    effect: "La dépense redevient une dépense directe ordinaire, validée, dans la catégorie choisie." },
];
const DEST_LABEL = Object.fromEntries(DESTINATIONS.map((d) => [d.value, d.label])) as Record<string, string>;

/** Enregistrements auxquels une pièce peut être rattachée (`SOURCE_MODELS` côté API). */
const ATTACH_SOURCES = [
  { value: "fuel_log", label: "Plein de carburant", endpoint: "/fuel/" },
  { value: "electric_charge", label: "Recharge électrique", endpoint: "/electric-charges/" },
  { value: "maintenance", label: "Maintenance", endpoint: "/maintenance/" },
  { value: "insurance", label: "Assurance", endpoint: "/vehicle-insurances/" },
  { value: "inspection", label: "Visite technique", endpoint: "/vehicle-inspections/" },
  { value: "revision", label: "Révision", endpoint: "/vehicle-revisions/" },
  { value: "vehicle_charge", label: "Charge véhicule", endpoint: "/finance/vehicle-charges/" },
];

const CHARGE_KINDS = [
  { value: "tyres", label: "Pneumatiques" }, { value: "subscription", label: "Abonnement (télématique, parking…)" },
  { value: "tax", label: "Taxe / vignette" }, { value: "other", label: "Autre charge fixe" },
];

const NATURES = [
  { value: "corrective", label: "Maintenance corrective" }, { value: "preventive", label: "Maintenance préventive" },
];

/** Catégories non spécialisées : carburant, maintenance et assurance vivent dans leurs tables. */
const GENERIC_CATEGORIES = Object.entries(CATEGORY_LABEL)
  .filter(([key]) => !["fuel", "maintenance", "insurance"].includes(key))
  .map(([value, label]) => ({ value, label }));

interface ReconcileResult extends LegacyExpense {
  reconciled_at: string;
  reconciliation: { destination: string; via?: string; adjustment?: string; category?: string };
}

type Fields = Record<string, string>;

function categoryLabel(row: LegacyExpense) {
  return CATEGORY_LABEL[row.original_category] ?? row.category_display;
}

function day(value: string) {
  const [y, m, d] = value.slice(0, 10).split("-").map(Number);
  return Date.UTC(y, (m || 1) - 1, d || 1);
}

/** Libellé lisible d'un enregistrement source, quel que soit son modèle (date, montant, détail). */
function describeRecord(r: Record<string, unknown>): { date: string | null; amount: string | null; text: string } {
  const pick = (...keys: string[]) => {
    for (const k of keys) if (r[k] != null && r[k] !== "") return String(r[k]);
    return null;
  };
  const date = pick("date", "performed_date", "scheduled_date", "start_date", "last_date", "next_date", "period_start");
  const amount = pick("amount", "cost", "total_cost");
  const detail = pick("label", "company", "provider", "center", "station", "notes");
  const text = [formatDate(date), amount != null ? money(amount) : null, detail].filter(Boolean).join(" · ");
  return { date, amount, text };
}

/** Enregistrements existants du véhicule pour le type choisi, les plus proches de la date de
 *  la dépense en tête (c'est presque toujours le bon). */
function useSourceRecords(sourceType: string, vehicle: string | null) {
  const source = ATTACH_SOURCES.find((s) => s.value === sourceType);
  return useQuery({
    queryKey: ["reconcile-sources", sourceType, vehicle],
    enabled: !!source && !!vehicle,
    queryFn: async () => {
      const { data } = await api.get<{ results: Record<string, unknown>[] } | Record<string, unknown>[]>(
        source!.endpoint, { params: { vehicle, page_size: "100" } });
      return Array.isArray(data) ? data : data.results;
    },
  });
}

function AttachFields({ row, fields, set }: { row: LegacyExpense; fields: Fields; set: (k: string, v: string) => void }) {
  const records = useSourceRecords(fields.source_type ?? "", row.vehicle);
  const options = useMemo(() => {
    const target = day(row.date);
    return (records.data ?? [])
      .map((r) => ({ id: String(r.id), ...describeRecord(r) }))
      .sort((a, b) => Math.abs((a.date ? day(a.date) : 0) - target) - Math.abs((b.date ? day(b.date) : 0) - target));
  }, [records.data, row.date]);

  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <div>
        <Label htmlFor="rec-source-type">Type d&apos;enregistrement</Label>
        <Select id="rec-source-type" value={fields.source_type ?? ""}
                onChange={(e) => { set("source_type", e.target.value); set("source_id", ""); }}>
          <option value="">Choisir…</option>
          {ATTACH_SOURCES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
        </Select>
      </div>
      <div>
        <Label htmlFor="rec-source-id">Enregistrement</Label>
        {row.vehicle && fields.source_type ? (
          records.isLoading ? (
            <div className="flex h-10 items-center"><Spinner className="h-4 w-4" /></div>
          ) : records.isError ? (
            // Liste illisible (droits) : on garde la saisie directe, l'API vérifiera la source.
            <>
              <Input id="rec-source-id" value={fields.source_id ?? ""} onChange={(e) => set("source_id", e.target.value)}
                     placeholder="Identifiant de l'enregistrement" />
              <p className="mt-1 text-[11px] text-red-600">{apiError(records.error)}</p>
            </>
          ) : options.length === 0 ? (
            <p className="py-2 text-xs text-faint">Aucun enregistrement de ce type pour ce véhicule.</p>
          ) : (
            <Select id="rec-source-id" value={fields.source_id ?? ""} onChange={(e) => set("source_id", e.target.value)}>
              <option value="">Choisir…</option>
              {options.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.text || o.id}{o.amount != null && Number(o.amount) === Number(row.amount) ? " · même montant" : ""}
                </option>
              ))}
            </Select>
          )
        ) : (
          <Input id="rec-source-id" value={fields.source_id ?? ""} onChange={(e) => set("source_id", e.target.value)}
                 placeholder="Identifiant de l'enregistrement" />
        )}
      </div>
      {!row.vehicle && (
        <p className="text-[11px] text-faint sm:col-span-2">
          Dépense sans véhicule : saisissez l&apos;identifiant de l&apos;enregistrement existant.
        </p>
      )}
    </div>
  );
}

function MaintenanceFields({ fields, set }: { fields: Fields; set: (k: string, v: string) => void }) {
  const types = useMaintenanceTypes();
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <div>
        <Label htmlFor="rec-nature">Nature</Label>
        <Select id="rec-nature" value={fields.nature ?? "corrective"} onChange={(e) => set("nature", e.target.value)}>
          {NATURES.map((n) => <option key={n.value} value={n.value}>{n.label}</option>)}
        </Select>
      </div>
      <div>
        <Label htmlFor="rec-mtype">Type de maintenance (facultatif)</Label>
        <Select id="rec-mtype" value={fields.maintenance_type ?? ""} onChange={(e) => set("maintenance_type", e.target.value)}>
          <option value="">—</option>
          {(types.data ?? []).map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
        </Select>
      </div>
    </div>
  );
}

function DestinationFields({ row, destination, fields, set }: {
  row: LegacyExpense; destination: Destination; fields: Fields; set: (k: string, v: string) => void;
}) {
  const input = (key: string, label: string, props: React.InputHTMLAttributes<HTMLInputElement> = {}) => (
    <div>
      <Label htmlFor={`rec-${key}`}>{label}</Label>
      <Input id={`rec-${key}`} value={fields[key] ?? ""} onChange={(e) => set(key, e.target.value)} {...props} />
    </div>
  );

  switch (destination) {
    case "attach":
      return <AttachFields row={row} fields={fields} set={set} />;
    case "fuel_log":
      return <div className="grid gap-3 sm:grid-cols-2">{input("liters", "Litres", { type: "number", min: "0", step: "0.01" })}</div>;
    case "electric_charge":
      return <div className="grid gap-3 sm:grid-cols-2">{input("kwh_recharged", "kWh rechargés", { type: "number", min: "0", step: "0.01" })}</div>;
    case "maintenance":
      return <MaintenanceFields fields={fields} set={set} />;
    case "insurance":
      return (
        <div className="grid gap-3 sm:grid-cols-3">
          {input("company", "Compagnie")}
          {input("start_date", "Début", { type: "date" })}
          {input("expiry_date", "Expiration", { type: "date" })}
        </div>
      );
    case "vehicle_charge":
      return (
        <div className="grid gap-3 sm:grid-cols-3">
          <div>
            <Label htmlFor="rec-kind">Nature</Label>
            <Select id="rec-kind" value={fields.kind ?? ""} onChange={(e) => set("kind", e.target.value)}>
              <option value="">Choisir…</option>
              {CHARGE_KINDS.map((k) => <option key={k.value} value={k.value}>{k.label}</option>)}
            </Select>
          </div>
          {input("period_start", "Début de période", { type: "date" })}
          {input("period_end", "Fin de période", { type: "date" })}
        </div>
      );
    case "generic":
      return (
        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <Label htmlFor="rec-category">Catégorie</Label>
            <Select id="rec-category" value={fields.category ?? "other"} onChange={(e) => set("category", e.target.value)}>
              {GENERIC_CATEGORIES.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
            </Select>
          </div>
        </div>
      );
  }
}

/** Ce que l'API exige pour chaque destination (`_require`) : vérifié ici pour éviter un aller-retour,
 *  l'API reste juge. Retourne le message d'erreur ou "". */
function validate(row: LegacyExpense, destination: Destination, fields: Fields, adjustmentOnly: boolean): string {
  if (destination === "attach") {
    if (!fields.source_type || !fields.source_id) return "Précisez l'enregistrement existant (type et enregistrement).";
    return "";
  }
  if (adjustmentOnly) return "";
  const spec = DESTINATIONS.find((d) => d.value === destination)!;
  if (spec.needsVehicle && !row.vehicle) {
    return "Cette destination exige un véhicule : la dépense n'en a pas. Choisissez « dépense générique » ou un rattachement.";
  }
  const positive = (key: string) => fields[key] && Number(fields[key]) > 0;
  switch (destination) {
    case "fuel_log": return positive("liters") ? "" : "Indiquez le nombre de litres.";
    case "electric_charge": return positive("kwh_recharged") ? "" : "Indiquez les kWh rechargés.";
    case "insurance":
      if (!fields.company?.trim() || !fields.start_date || !fields.expiry_date) return "Compagnie, début et expiration sont requis.";
      return fields.expiry_date < fields.start_date ? "L'expiration doit suivre le début." : "";
    case "vehicle_charge":
      if (!fields.kind || !fields.period_start || !fields.period_end) return "Nature et période (début, fin) sont requises.";
      return fields.period_end < fields.period_start ? "La fin de période doit suivre son début." : "";
    default: return "";
  }
}

/** Paramètres envoyés à l'API : seulement ceux de la destination choisie. */
function paramsFor(destination: Destination, fields: Fields): Record<string, unknown> {
  const keys: Record<Destination, string[]> = {
    attach: ["source_type", "source_id"], fuel_log: ["liters"], electric_charge: ["kwh_recharged"],
    maintenance: ["nature", "maintenance_type"], insurance: ["company", "start_date", "expiry_date"],
    vehicle_charge: ["kind", "period_start", "period_end"], generic: ["category"],
  };
  const defaults: Fields = { nature: "corrective", category: "other" };
  const out: Record<string, unknown> = {};
  for (const key of keys[destination]) {
    const value = (fields[key] ?? defaults[key] ?? "").trim();
    if (value) out[key] = value;
  }
  return out;
}

function RecapLine({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-3">
      <dt className="text-muted">{label}</dt>
      <dd className="text-right font-medium text-ink">{children}</dd>
    </div>
  );
}

function ReconcileModal({ row, closedMonth, onClose, onDone }: {
  row: LegacyExpense; closedMonth: boolean | null; onClose: () => void; onDone: (result: ReconcileResult) => void;
}) {
  const proposed = (DEST_LABEL[row.proposed_destination] ? row.proposed_destination : "generic") as Destination;
  const [destination, setDestination] = useState<Destination>(proposed);
  const [fields, setFields] = useState<Fields>({ start_date: row.date, period_start: row.date });
  const [step, setStep] = useState<"form" | "recap">("form");
  const [error, setError] = useState("");
  const reconcile = useReconcile();
  const spec = DESTINATIONS.find((d) => d.value === destination)!;
  const month = row.date.slice(0, 7);
  // Mois clos (connu) : hors rattachement, rien n'est écrit dans ce mois — le montant passe par
  // un ajustement à approuver, les champs propres à la destination ne servent donc pas.
  const adjustmentOnly = closedMonth === true && destination !== "attach";

  function set(key: string, value: string) {
    setFields((f) => ({ ...f, [key]: value }));
  }

  function review() {
    const message = validate(row, destination, fields, adjustmentOnly);
    setError(message);
    if (!message) setStep("recap");
  }

  function confirm() {
    setError("");
    reconcile.mutate({ id: row.id, destination, params: paramsFor(destination, fields) }, {
      onSuccess: (data) => onDone(data as ReconcileResult),
      onError: (e) => setError(apiError(e)),
    });
  }

  const params = paramsFor(destination, fields);
  const sourceLabel = ATTACH_SOURCES.find((s) => s.value === params.source_type)?.label;

  return (
    <Modal open title="Réconcilier une dépense historique" onClose={onClose} className="sm:max-w-xl">
      <div className="max-h-[70vh] space-y-4 overflow-y-auto text-sm">
        {step === "form" ? (
          <>
            <p className="rounded-lg bg-surface2 px-3 py-2 text-xs text-muted">
              <b className="text-ink">{row.label || "Sans libellé"}</b> · {categoryLabel(row)} · {money(row.amount)} · {formatDate(row.date)}
              {row.vehicle_registration ? ` · ${row.vehicle_registration}` : ""}
            </p>
            <div>
              <Label htmlFor="rec-destination">Destination</Label>
              <Select id="rec-destination" value={destination}
                      onChange={(e) => { setDestination(e.target.value as Destination); setError(""); }}>
                {DESTINATIONS.map((d) => (
                  <option key={d.value} value={d.value}>
                    {d.label}{d.value === proposed ? " (proposée)" : ""}{d.needsVehicle && !row.vehicle ? " — exige un véhicule" : ""}
                  </option>
                ))}
              </Select>
              <p className="mt-1 text-[11px] text-faint">{spec.effect}</p>
            </div>
            {adjustmentOnly ? (
              <p className="flex gap-1.5 rounded-lg border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-800">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                Le mois {month} est clos : la reprise ne s&apos;écrira pas dans ce mois. Un ajustement financier du
                montant sera créé sur la période ouverte, à approuver par une autre personne.
              </p>
            ) : (
              <DestinationFields row={row} destination={destination} fields={fields} set={set} />
            )}
          </>
        ) : (
          <>
            <dl className="space-y-1.5 rounded-lg border border-line bg-surface2 p-3 text-xs">
              <RecapLine label="Ancienne catégorie">{categoryLabel(row)}</RecapLine>
              <RecapLine label="Montant">{money(row.amount)}</RecapLine>
              <RecapLine label="Date">{formatDate(row.date)}</RecapLine>
              <RecapLine label="Véhicule">{row.vehicle_registration ?? "—"}</RecapLine>
              <RecapLine label="Description">{row.label || "—"}</RecapLine>
              <RecapLine label="Filiale">{row.subsidiary_name}</RecapLine>
              <RecapLine label="Destination proposée">{DEST_LABEL[row.proposed_destination] ?? row.proposed_destination}</RecapLine>
              <RecapLine label="Destination choisie">
                <span className={destination !== proposed ? "text-amber-700" : undefined}>{spec.label}</span>
              </RecapLine>
              {!adjustmentOnly && destination === "attach" && (
                <RecapLine label="Enregistrement">{sourceLabel ?? "—"} · {String(params.source_id ?? "—")}</RecapLine>
              )}
              {!adjustmentOnly && destination === "fuel_log" && <RecapLine label="Litres">{formatNumber(String(params.liters), "L")}</RecapLine>}
              {!adjustmentOnly && destination === "electric_charge" && <RecapLine label="kWh rechargés">{formatNumber(String(params.kwh_recharged), "kWh")}</RecapLine>}
              {!adjustmentOnly && destination === "maintenance" && (
                <RecapLine label="Nature">{NATURES.find((n) => n.value === params.nature)?.label ?? "—"}</RecapLine>
              )}
              {!adjustmentOnly && destination === "insurance" && (
                <RecapLine label="Police">{String(params.company)} · {formatDate(String(params.start_date))} → {formatDate(String(params.expiry_date))}</RecapLine>
              )}
              {!adjustmentOnly && destination === "vehicle_charge" && (
                <RecapLine label="Charge">
                  {CHARGE_KINDS.find((k) => k.value === params.kind)?.label} · {formatDate(String(params.period_start))} → {formatDate(String(params.period_end))}
                </RecapLine>
              )}
              {!adjustmentOnly && destination === "generic" && (
                <RecapLine label="Nouvelle catégorie">{CATEGORY_LABEL[String(params.category)] ?? String(params.category)}</RecapLine>
              )}
            </dl>
            <p className="flex gap-1.5 rounded-lg border border-brand-500/30 bg-brand-500/5 px-3 py-2 text-xs text-ink">
              <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-brand-600" />
              <span>
                <b>Le montant ne sera compté qu&apos;une fois.</b> {adjustmentOnly ? "" : spec.effect} La dépense d&apos;origine
                est conservée (catégorie d&apos;origine, auteur et date de la reprise sont tracés).
              </span>
            </p>
            {destination !== "attach" && (
              <p className="flex gap-1.5 rounded-lg border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-800">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                {closedMonth
                  ? `Mois ${month} clos : rien n'est écrit dans ce mois. Un ajustement financier de ${money(row.amount)} est créé, comptabilisé sur la période ouverte et rattaché à ${month} ; il devra être approuvé par une autre personne (onglet Ajustements).`
                  : `Si le mois ${month} est clos, la reprise ne s'écrit pas dans le passé : elle devient un ajustement financier à approuver par une autre personne, comptabilisé sur la période ouverte.`}
              </p>
            )}
          </>
        )}
        {error && <p className="text-xs text-red-600">{error}</p>}
      </div>
      <div className="flex justify-end gap-2 pt-3">
        {step === "form" ? (
          <>
            <Button variant="secondary" onClick={onClose}>Annuler</Button>
            <Button onClick={review}>Vérifier <ArrowRight className="h-4 w-4" /></Button>
          </>
        ) : (
          <>
            <Button variant="secondary" onClick={() => { setStep("form"); setError(""); }} disabled={reconcile.isPending}>Modifier</Button>
            <Button onClick={confirm} disabled={reconcile.isPending}>
              {reconcile.isPending ? <Spinner className="h-4 w-4" /> : <CheckCircle2 className="h-4 w-4" />} Confirmer la reprise
            </Button>
          </>
        )}
      </div>
    </Modal>
  );
}

/** Finance → Réconciliation (F2) : dépenses « carburant / maintenance / assurance » saisies
 *  avant D2, exclues des coûts tant qu'elles ne sont pas reprises. Chaque reprise décide où vit
 *  le coût, pour qu'il ne soit compté qu'une fois ; réservée aux profils qui valident. */
export function ReconciliationPanel({ subsidiary }: { subsidiary: string }) {
  const { me } = useAuth();
  const canReconcile = canFinance(me, "validate_expense");
  const legacy = useLegacyExpenses();
  const periods = useFinancePeriods();
  const [search, setSearch] = useState("");
  const [current, setCurrent] = useState<LegacyExpense | null>(null);
  const [notice, setNotice] = useState<{ tone: "ok" | "warn"; text: string } | null>(null);

  // La liste suit le périmètre de l'utilisateur côté API ; le sélecteur de filiale la restreint.
  const rows = useMemo(() => {
    const q = search.trim().toLowerCase();
    return (legacy.data ?? []).filter((r) => (!subsidiary || r.subsidiary === subsidiary) && (!q || [
      r.label, r.vehicle_registration ?? "", r.subsidiary_name, categoryLabel(r),
    ].some((v) => v.toLowerCase().includes(q))));
  }, [legacy.data, subsidiary, search]);
  const total = rows.reduce((sum, r) => sum + Number(r.amount), 0);

  /** null = statut inconnu (mois hors des 12 derniers mois listés). */
  function closedMonth(row: LegacyExpense): boolean | null {
    const period = periods.data?.find((p) => p.period === row.date.slice(0, 7));
    return period ? period.status === "closed" : null;
  }

  function done(result: ReconcileResult) {
    const row = current;
    setCurrent(null);
    const name = `« ${row?.label || "dépense"} »`;
    if (result.reconciliation?.via === "adjustment") {
      setNotice({ tone: "warn", text: `${name} : mois clos, un ajustement financier a été créé. Il doit être approuvé par une autre personne (onglet Ajustements) avant d'être compté.` });
    } else {
      setNotice({ tone: "ok", text: `${name} reprise : ${DEST_LABEL[result.reconciliation?.destination ?? ""] ?? "destination enregistrée"}. Le montant n'est compté qu'une fois.` });
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <div className="relative w-full sm:w-72">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-faint" />
          <Input value={search} onChange={(e) => setSearch(e.target.value)} className="pl-9"
                 placeholder="Rechercher (libellé, véhicule, filiale…)" aria-label="Rechercher" />
        </div>
        {legacy.data && (
          <p className="text-xs text-muted sm:ml-auto">
            {formatNumber(rows.length)} dépense(s) à reprendre · {money(String(total))} exclus des coûts
          </p>
        )}
      </div>

      <p className="flex gap-1.5 rounded-lg border border-line bg-surface2 px-3 py-2 text-xs text-muted">
        <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
        Ces dépenses recouvrent une table dédiée (plein, maintenance, assurance) : elles sont exclues des coûts tant
        qu&apos;elles ne sont pas reprises, pour ne pas être comptées deux fois. Une dépense d&apos;un mois clos devient
        un ajustement à approuver, comptabilisé sur la période ouverte.
        {!canReconcile && " Lecture seule : la reprise est réservée aux profils qui valident les dépenses."}
      </p>

      {notice && (
        <p className={notice.tone === "ok"
          ? "flex gap-1.5 rounded-lg border border-emerald-500/30 bg-emerald-500/5 px-3 py-2 text-xs text-emerald-700"
          : "flex gap-1.5 rounded-lg border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-800"}>
          {notice.tone === "ok" ? <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0" /> : <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />}
          {notice.text}
        </p>
      )}

      <Card>
        <CardBody className="p-0">
          {legacy.isLoading ? (
            <div className="flex justify-center py-12"><Spinner className="h-6 w-6" /></div>
          ) : legacy.isError ? (
            <EmptyState title="Liste indisponible" hint={apiError(legacy.error)} />
          ) : rows.length === 0 ? (
            <EmptyState title={search ? "Aucune dépense ne correspond" : "Aucune dépense historique à reprendre"}
                        hint={search ? undefined : "Toutes les anciennes dépenses ont été réconciliées."} />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-faint">
                    <th className="px-4 py-2.5 font-medium">Ancienne catégorie</th>
                    <th className="px-4 py-2.5 text-right font-medium">Montant</th>
                    <th className="px-4 py-2.5 font-medium">Date</th>
                    <th className="px-4 py-2.5 font-medium">Véhicule</th>
                    <th className="px-4 py-2.5 font-medium">Description</th>
                    <th className="px-4 py-2.5 font-medium">Filiale</th>
                    <th className="px-4 py-2.5 font-medium">Destination proposée</th>
                    {canReconcile && <th className="px-4 py-2.5" />}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.id} className="border-b border-line last:border-0">
                      <td className="whitespace-nowrap px-4 py-2.5 text-ink">{categoryLabel(r)}</td>
                      <td className="whitespace-nowrap px-4 py-2.5 text-right font-medium text-ink">{money(r.amount)}</td>
                      <td className="whitespace-nowrap px-4 py-2.5 text-muted">
                        {formatDate(r.date)}
                        {closedMonth(r) && <span className="ml-1.5 rounded bg-slate-500/10 px-1.5 py-0.5 text-[10px] text-slate-600">mois clos</span>}
                      </td>
                      <td className="whitespace-nowrap px-4 py-2.5 text-muted">{r.vehicle_registration ?? "—"}</td>
                      <td className="max-w-[16rem] truncate px-4 py-2.5 text-muted" title={r.label}>{r.label || "—"}</td>
                      <td className="whitespace-nowrap px-4 py-2.5 text-muted">{r.subsidiary_name}</td>
                      <td className="whitespace-nowrap px-4 py-2.5 text-muted">{DEST_LABEL[r.proposed_destination] ?? r.proposed_destination}</td>
                      {canReconcile && (
                        <td className="whitespace-nowrap px-4 py-2.5 text-right">
                          <Button size="sm" variant="secondary" onClick={() => { setNotice(null); setCurrent(r); }}>Réconcilier</Button>
                        </td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardBody>
      </Card>

      {current && canReconcile && (
        <ReconcileModal key={current.id} row={current} closedMonth={closedMonth(current)}
                        onClose={() => setCurrent(null)} onDone={done} />
      )}
    </div>
  );
}
