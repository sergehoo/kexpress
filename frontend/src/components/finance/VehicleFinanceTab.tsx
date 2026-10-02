"use client";

import { useState } from "react";
import { Plus } from "lucide-react";

import { Button, EmptyState, Select, Spinner } from "@/components/ui";
import { EntityForm, type Field } from "@/components/EntityForm";
import { RowActions } from "@/components/RowActions";
import { money } from "@/components/finance/RealCostPanel";
import {
  useFinancePeriods, useVehicleAcquisition, useVehicleCharges, useVehicleCost,
  type Money, type VehicleCharge, type VehicleCostRow,
} from "@/lib/queries";
import { useCrud } from "@/lib/crud";
import { apiError } from "@/lib/api";
import { cn, formatDate, formatNumber } from "@/lib/utils";

/** Mois véhicule F2 : la ligne F1 + dépenses exceptionnelles, ajustements et cumul. */
type VehicleMonthF2 = VehicleCostRow & {
  /** Dépenses directes comptées + maintenance rattachée à une course (hors charges fixes). */
  exceptional_expenses: Money;
  /** Ajustements APPROUVÉS comptabilisés sur ce mois. */
  adjustments: Money;
  /** Mois clos jusqu'à celui-ci, plus ce mois s'il est encore provisoire. */
  cumulative_cost: Money;
};

const MODES = [
  { value: "purchase", label: "Achat" }, { value: "credit", label: "Crédit" },
  { value: "leasing", label: "Leasing" }, { value: "rental", label: "Location longue durée" },
];
const RENT = ["leasing", "rental"];
const KINDS = [
  { value: "tyres", label: "Pneumatiques" }, { value: "subscription", label: "Abonnement (télématique, parking…)" },
  { value: "tax", label: "Taxe / vignette" }, { value: "other", label: "Autre charge fixe" },
];
// Composantes des charges fixes : l'assurance, l'amortissement, la maintenance et les pneus
// d'abord (spec 14), puis le reste.
const COMPONENTS: [keyof VehicleCostRow["components"], string][] = [
  ["insurance", "Assurance"], ["depreciation", "Amortissement / loyer"], ["maintenance", "Maintenance"],
  ["tyres", "Pneus"], ["subscriptions", "Abonnements"], ["taxes", "Taxes"], ["other_fixed", "Autres (dont visite)"],
];

// Copie locale : la table de RealCostPanel n'est pas exportée.
const MISSING: Record<string, string> = {
  energy: "énergie", driver: "chauffeur", distance: "distance", insurance: "assurance",
  insurance_period: "période d'assurance", acquisition: "acquisition", monthly_payment: "loyer",
  depreciation_basis: "base d'amortissement", maintenance: "maintenance", tyres: "pneumatiques",
  subscriptions: "abonnements", taxes: "taxes", other_fixed: "autres charges",
};

function Tile({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: string }) {
  return (
    <div className="rounded-lg border border-line bg-surface2/60 px-3 py-2">
      <p className="text-[11px] uppercase tracking-wide text-faint">{label}</p>
      <p className={cn("text-base font-semibold text-ink", tone)}>{value}</p>
      {sub && <p className="text-[11px] text-faint">{sub}</p>}
    </div>
  );
}

function Line({ label, value, strong, indent, tone }: {
  label: string; value: string; strong?: boolean; indent?: boolean; tone?: string;
}) {
  return (
    <li className={cn("flex justify-between gap-3 border-b border-line py-1", indent && "pl-4")}>
      <span className={cn(indent ? "text-faint" : "text-muted", strong && "font-semibold text-ink")}>{label}</span>
      <b className={cn("text-ink", tone)}>{value}</b>
    </li>
  );
}

const ACQUISITION_FIELDS: Field[] = [
  { name: "mode", label: "Mode d'acquisition", type: "select", required: true, options: MODES },
  { name: "acquisition_date", label: "Date d'acquisition / début", type: "date", required: true },
  { name: "purchase_price", label: "Prix d'acquisition", type: "number", min: 0, step: "0.01",
    hidden: (v) => RENT.includes(String(v.mode)) },
  { name: "residual_value", label: "Valeur résiduelle", type: "number", min: 0, step: "0.01",
    hidden: (v) => RENT.includes(String(v.mode)) },
  { name: "depreciation_months", label: "Durée (mois)", type: "number", min: 1 },
  { name: "monthly_payment", label: "Loyer mensuel", type: "number", min: 0, step: "0.01",
    hidden: (v) => !RENT.includes(String(v.mode)) },
  { name: "normative_monthly_km", label: "Capacité normative (km / mois)", type: "number", min: 1,
    placeholder: "Défaut flotte si vide" },
  { name: "notes", label: "Notes", type: "textarea", full: true },
];

const CHARGE_FIELDS: Field[] = [
  { name: "kind", label: "Nature", type: "select", required: true, options: KINDS },
  { name: "label", label: "Libellé", required: true },
  { name: "amount", label: "Montant", type: "number", required: true, min: 0, step: "0.01" },
  { name: "period_start", label: "Début de période", type: "date", required: true },
  { name: "period_end", label: "Fin de période", type: "date", required: true },
  { name: "supplier", label: "Fournisseur" },
  { name: "notes", label: "Notes", type: "textarea", full: true },
];

/** Onglet Finance d'un véhicule : réservé aux profils `view_vehicle_cost` de la filiale
 *  propriétaire (l'API refuse les autres). Assurance et visite restent dans leurs onglets :
 *  les ressaisir ici les compterait deux fois. */
export function VehicleFinanceTab({ vehicleId, canWrite }: { vehicleId: string; canWrite: boolean }) {
  const periods = useFinancePeriods();
  const [period, setPeriod] = useState("");
  const selected = period || periods.data?.[0]?.period || "";
  const cost = useVehicleCost(vehicleId, selected);
  const acquisition = useVehicleAcquisition(vehicleId);
  const charges = useVehicleCharges(vehicleId);
  const crudAcq = useCrud("finance/vehicle-acquisitions", ["vehicle-cost", "vehicle-costs"]);
  const crudCharge = useCrud("finance/vehicle-charges", ["vehicle-cost", "vehicle-costs"]);
  const [form, setForm] = useState<null | { kind: "acquisition" } | { kind: "charge"; row?: VehicleCharge }>(null);
  const [error, setError] = useState("");

  function submit(values: Record<string, unknown>) {
    setError("");
    for (const k of Object.keys(values)) if (values[k] === "") values[k] = null;
    const opts = { onSuccess: () => setForm(null), onError: (e: unknown) => setError(apiError(e)) };
    if (form?.kind === "acquisition") {
      const body = { ...values, vehicle: vehicleId };
      if (acquisition.data) crudAcq.update.mutate({ id: acquisition.data.id, body }, opts);
      else crudAcq.create.mutate(body, opts);
    } else if (form?.kind === "charge") {
      const body = { ...values, vehicle: vehicleId };
      if (form.row) crudCharge.update.mutate({ id: form.row.id, body }, opts);
      else crudCharge.create.mutate(body, opts);
    }
  }

  const c = cost.data as VehicleMonthF2 | undefined;
  const a = acquisition.data;
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        <Select value={selected} onChange={(e) => setPeriod(e.target.value)} className="sm:w-44">
          {(periods.data ?? []).map((p) => (
            <option key={p.period} value={p.period}>{p.period}{p.status === "closed" ? " · clos" : ""}</option>
          ))}
        </Select>
        {c && <span className="text-xs text-muted">{c.provisional ? "Provisoire (mois ouvert)" : "Figé (mois clos)"}</span>}
      </div>

      {cost.isLoading ? <div className="flex justify-center py-8"><Spinner /></div> : c && (
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <Tile label="Coût période" value={money(c.total_cost)} sub={c.provisional ? "provisoire" : "figé"} />
            <Tile label="Coût cumulé" value={money(c.cumulative_cost)}
                  sub={c.provisional ? `mois clos + ${c.period} provisoire` : `mois clos jusqu'à ${c.period}`} />
            <Tile label="Coût / km" value={money(c.cost_per_km)} />
            <Tile label="Coût / course" value={money(c.cost_per_trip)} sub={`${c.trips} course(s)`} />
          </div>
          <div className="grid gap-4 lg:grid-cols-2">
            {/* Composition : coût période = énergie + charges fixes + dépenses exceptionnelles
                + ajustements approuvés (même somme que l'API, chaque coût une seule fois). */}
            <ul className="space-y-1 text-xs">
              <Line label="Énergie" value={money(c.energy_cost)} />
              <Line label="Charges fixes" value={money(c.fixed_cost)} />
              {COMPONENTS.map(([key, label]) => (
                <Line key={key} indent label={label} value={money(c.components[key])} />
              ))}
              <Line label="Dépenses exceptionnelles" value={money(c.exceptional_expenses)} />
              <Line label="Ajustements" value={money(c.adjustments)} />
              <Line strong label="Coût période" value={money(c.total_cost)} />
            </ul>
            <div className="space-y-2">
              <div className="grid grid-cols-2 gap-2 rounded-lg bg-surface2 p-3 text-xs text-muted">
                <span className="col-span-2">Sous-utilisation : <b className="text-rose-600">{money(c.under_utilisation_cost)}</b>
                  {" "}· utilisation {c.utilisation_rate != null ? `${Math.round(Number(c.utilisation_rate) * 100)} %` : "—"}
                  {" "}({formatNumber(c.used_km ?? 0)} / {formatNumber(c.normative_km ?? 0)} km)</span>
                <span>Absorbé par {c.trips} course(s) : <b className="text-ink">{money(c.absorbed_cost)}</b></span>
                <span>Coût en charge : <b className="text-ink">{money(c.loaded_cost)}</b></span>
                <span>Coût à vide : <b className="text-ink">{money(c.empty_cost)}</b></span>
              </div>
              {c.missing.length > 0 && (
                <p className="rounded-lg border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-[11px] text-amber-700">
                  inconnu : {c.missing.map((m) => MISSING[m] ?? m).join(", ")} — non compté, jamais à 0.
                </p>
              )}
              <p className="text-[11px] text-faint">
                Dépenses exceptionnelles : dépenses directes validées et maintenance rattachée à une course.
                Ajustements : corrections approuvées comptabilisées sur ce mois (le mois d&apos;origine reste figé).
              </p>
            </div>
          </div>
        </div>
      )}

      <div className="rounded-lg border border-line p-3">
        <div className="mb-2 flex items-center justify-between">
          <h4 className="text-sm font-semibold text-ink">Acquisition</h4>
          {canWrite && <Button variant="secondary" onClick={() => { setError(""); setForm({ kind: "acquisition" }); }}>
            {a ? "Modifier" : "Renseigner"}</Button>}
        </div>
        {a ? (
          <p className="text-xs text-muted">
            {a.mode_display} le {formatDate(a.acquisition_date)}
            {a.purchase_price != null && <> · prix {money(a.purchase_price, a.currency)} · résiduel {money(a.residual_value, a.currency)}</>}
            {a.monthly_payment != null && <> · loyer {money(a.monthly_payment, a.currency)}/mois</>}
            {a.depreciation_months != null && <> · {a.depreciation_months} mois</>}
            {a.normative_monthly_km != null && <> · capacité {formatNumber(a.normative_monthly_km)} km/mois</>}
          </p>
        ) : <p className="text-xs text-faint">Non renseignée : l&apos;amortissement du véhicule est inconnu.</p>}
      </div>

      <div className="rounded-lg border border-line p-3">
        <div className="mb-2 flex items-center justify-between">
          <h4 className="text-sm font-semibold text-ink">Charges fixes</h4>
          {canWrite && <Button variant="secondary" onClick={() => { setError(""); setForm({ kind: "charge" }); }}>
            <Plus className="h-4 w-4" /> Ajouter</Button>}
        </div>
        {!charges.data?.length ? <EmptyState title="Aucune charge fixe" hint="Pneumatiques, abonnements, taxes…" /> : (
          <ul className="divide-y divide-line text-sm">
            {charges.data.map((ch) => (
              <li key={ch.id} className="flex items-center gap-3 py-2">
                <div className="min-w-0 flex-1">
                  <p className="font-medium text-ink">{ch.label} <span className="text-xs font-normal text-muted">· {ch.kind_display}</span></p>
                  <p className="text-[11px] text-muted">{formatDate(ch.period_start)} → {formatDate(ch.period_end)}{ch.supplier ? ` · ${ch.supplier}` : ""}</p>
                </div>
                <b className="text-ink">{money(ch.amount)}</b>
                {canWrite && <RowActions label={ch.label} deleting={crudCharge.remove.isPending}
                  onEdit={() => { setError(""); setForm({ kind: "charge", row: ch }); }}
                  onDelete={() => crudCharge.remove.mutate(ch.id, { onError: (e) => window.alert(apiError(e)) })} />}
              </li>
            ))}
          </ul>
        )}
      </div>

      {form && (
        <EntityForm
          open
          title={form.kind === "acquisition" ? "Acquisition du véhicule" : form.row ? "Modifier la charge" : "Nouvelle charge fixe"}
          fields={form.kind === "acquisition" ? ACQUISITION_FIELDS : CHARGE_FIELDS}
          initial={form.kind === "acquisition"
            ? ((a as unknown as Record<string, unknown>) ?? { mode: "purchase" })
            : ((form.row as unknown as Record<string, unknown>) ?? { kind: "tyres" })}
          submitting={crudAcq.create.isPending || crudAcq.update.isPending || crudCharge.create.isPending || crudCharge.update.isPending}
          error={error}
          onClose={() => setForm(null)}
          onSubmit={submit}
        />
      )}
    </div>
  );
}
