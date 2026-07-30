"use client";

import { useState } from "react";
import { BatteryCharging, Droplet, Fuel as FuelIcon, Plug, Plus, Wallet, Zap } from "lucide-react";

import { Button, Card, CardBody, EmptyState, Spinner } from "@/components/ui";
import { EntityForm, type Field } from "@/components/EntityForm";
import { RowActions } from "@/components/RowActions";
import { useElectricCharges, useFuel, useSubsidiaries, useVehicles } from "@/lib/queries";
import { useCrud } from "@/lib/crud";
import { useAuth } from "@/lib/auth";
import { apiError } from "@/lib/api";
import type { ElectricCharge, FuelLog } from "@/lib/types";
import { cn, formatDate, formatNumber } from "@/lib/utils";

function Mini({ icon: Icon, label, value, tone }: { icon: React.ElementType; label: string; value: string; tone: string }) {
  return (
    <Card>
      <CardBody className="flex items-center gap-3 py-3">
        <span className={`flex h-10 w-10 items-center justify-center rounded-xl ${tone}`}><Icon className="h-5 w-5" /></span>
        <div><p className="text-lg font-semibold leading-none text-ink">{value}</p><p className="text-[11px] text-muted">{label}</p></div>
      </CardBody>
    </Card>
  );
}

type Section = "fuel" | "electric";

const SECTIONS: { key: Section; label: string; icon: React.ElementType }[] = [
  { key: "fuel", label: "Carburants", icon: FuelIcon },
  { key: "electric", label: "Électricité", icon: Zap },
];

/** Gestion de l'énergie (§12) — deux sections, deux unités.
 *
 *  La flotte mêle essence, gasoil, GPL, hybride et électrique : le module couvre donc
 *  « l'énergie », pas seulement le carburant. Les litres et les kWh restent dans leurs
 *  sections respectives — seuls les coûts sont comparables entre les deux. */
export default function EnergiePage() {
  const [section, setSection] = useState<Section>("fuel");

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-lg font-semibold text-ink">Gestion de l&apos;énergie</h2>
        <p className="text-sm text-muted">
          Carburants et électricité — consommations, coûts et traçabilité des relevés.
        </p>
      </div>

      <div className="flex w-fit rounded-lg border border-line bg-surface p-0.5">
        {SECTIONS.map(({ key, label, icon: Icon }) => (
          <button
            key={key}
            onClick={() => setSection(key)}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition-colors",
              section === key ? "bg-brand-600 text-white" : "text-muted hover:bg-surface2",
            )}
          >
            <Icon className="h-3.5 w-3.5" /> {label}
          </button>
        ))}
      </div>

      {section === "fuel" ? <CarburantsSection /> : <ElectriciteSection />}
    </div>
  );
}

// --- Carburants (§13) ------------------------------------------------------

type FuelModal = { mode: "create" } | { mode: "edit"; row: FuelLog } | null;

function CarburantsSection() {
  const { me } = useAuth();
  const [modal, setModal] = useState<FuelModal>(null);
  const [error, setError] = useState("");
  const { data, isLoading } = useFuel();
  const { data: vehicles } = useVehicles();
  const { data: subs } = useSubsidiaries();
  const crud = useCrud("fuel", ["dashboard-stats", "fuel-intel"]);
  const logs = data?.results ?? [];

  const totalLiters = logs.reduce((s, l) => s + Number(l.liters ?? 0), 0);
  const totalAmount = logs.reduce((s, l) => s + Number(l.amount ?? 0), 0);
  const avgPrice = totalLiters ? totalAmount / totalLiters : 0;

  const fields: Field[] = [
    { name: "vehicle", label: "Véhicule", type: "select", required: true,
      options: (vehicles?.results ?? [])
        .filter((v) => v.fuel_type !== "electric")
        .map((v) => ({ value: v.id, label: `${v.registration} — ${v.brand} ${v.model}` })) },
    { name: "date", label: "Date", type: "date", required: true },
    { name: "fuel_code", label: "Carburant", type: "select",
      options: [{ value: "super", label: "Super sans plomb" }, { value: "gasoil", label: "Gasoil" }] },
    { name: "liters", label: "Litres", type: "number", required: true, min: 0, step: "0.01" },
    { name: "amount", label: "Montant", type: "number", required: true, min: 0, step: "0.01" },
    { name: "price_per_liter", label: "Prix / litre", type: "number", min: 0, step: "0.01" },
    { name: "estimated_liters", label: "Litres estimés", type: "number", min: 0, step: "0.01" },
    { name: "station", label: "Station", type: "text" },
    { name: "mileage", label: "Km au plein", type: "number", min: 0 },
    ...(me?.has_company_scope
      ? [{ name: "subsidiary", label: "Filiale", type: "select" as const, required: true,
          options: (subs ?? []).map((s) => ({ value: s.id, label: s.name })) }]
      : []),
  ];

  function handleSubmit(values: Record<string, unknown>) {
    setError("");
    const opts = { onSuccess: () => setModal(null), onError: (e: unknown) => setError(apiError(e)) };
    if (modal?.mode === "edit") crud.update.mutate({ id: modal.row.id, body: values }, opts);
    else crud.create.mutate(values, opts);
  }

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <Mini icon={Droplet} label="Litres total" value={formatNumber(totalLiters, "L")} tone="bg-sky-500/10 text-sky-600" />
        <Mini icon={Wallet} label="Coût carburant total" value={formatNumber(totalAmount)} tone="bg-brand-500/10 text-brand-600" />
        <Mini icon={FuelIcon} label="Prix moyen / litre" value={formatNumber(Math.round(avgPrice))} tone="bg-amber-500/10 text-amber-600" />
      </div>

      <div className="flex">
        <Button className="ml-auto" onClick={() => { setError(""); setModal({ mode: "create" }); }}>
          <Plus className="h-4 w-4" /> Nouveau plein
        </Button>
      </div>

      <Card>
        <CardBody className="p-0">
          {isLoading ? (
            <div className="flex justify-center py-16"><Spinner className="h-7 w-7" /></div>
          ) : logs.length === 0 ? (
            <EmptyState title="Aucun plein enregistré" />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-faint">
                    <th className="px-5 py-3 font-medium">Date</th>
                    <th className="px-5 py-3 font-medium">Véhicule</th>
                    <th className="px-5 py-3 font-medium">Litres</th>
                    <th className="px-5 py-3 font-medium">Montant</th>
                    <th className="px-5 py-3 font-medium">Station</th>
                    <th className="px-5 py-3 font-medium">Écart est./réel</th>
                    <th className="px-5 py-3 font-medium">Km</th>
                    <th className="px-5 py-3 font-medium text-right">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {logs.map((l) => (
                    <tr key={l.id} className="hover:bg-surface2">
                      <td className="px-5 py-3 text-muted">{formatDate(l.date)}</td>
                      <td className="px-5 py-3 font-medium text-ink">{l.vehicle_registration}</td>
                      <td className="px-5 py-3 text-muted">{formatNumber(l.liters, "L")}</td>
                      <td className="px-5 py-3 text-muted">{formatNumber(l.amount)}</td>
                      <td className="px-5 py-3 text-muted">{l.station || "—"}</td>
                      <td className={cn(
                        "px-5 py-3",
                        l.variance_pct != null && Math.abs(l.variance_pct) >= 20
                          ? "font-semibold text-rose-600" : "text-muted",
                      )}>
                        {l.variance_pct != null ? `${l.variance_pct > 0 ? "+" : ""}${l.variance_pct} %` : "—"}
                      </td>
                      <td className="px-5 py-3 text-muted">{l.mileage ? formatNumber(l.mileage, "km") : "—"}</td>
                      <td className="px-5 py-3">
                        <RowActions
                          label={`plein ${l.vehicle_registration}`}
                          deleting={crud.remove.isPending}
                          onEdit={() => { setError(""); setModal({ mode: "edit", row: l }); }}
                          onDelete={() => crud.remove.mutate(l.id)}
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardBody>
      </Card>

      {modal && (
        <EntityForm
          open
          title={modal.mode === "edit" ? "Modifier le plein" : "Nouveau plein"}
          fields={fields}
          initial={modal.mode === "edit" ? (modal.row as unknown as Record<string, unknown>) : {}}
          submitting={crud.create.isPending || crud.update.isPending}
          error={error}
          onClose={() => setModal(null)}
          onSubmit={handleSubmit}
        />
      )}
    </div>
  );
}

// --- Électricité (§14) -----------------------------------------------------

type ChargeModal = { mode: "create" } | { mode: "edit"; row: ElectricCharge } | null;

function ElectriciteSection() {
  const { me } = useAuth();
  const [modal, setModal] = useState<ChargeModal>(null);
  const [error, setError] = useState("");
  const { data, isLoading } = useElectricCharges();
  const { data: vehicles } = useVehicles();
  const { data: subs } = useSubsidiaries();
  const crud = useCrud("electric-charges", ["dashboard-stats", "fuel-intel"]);
  const charges = data?.results ?? [];

  const electricVehicles = (vehicles?.results ?? []).filter((v) => v.fuel_type === "electric");
  const totalKwh = charges.reduce((s, c) => s + Number(c.kwh_recharged ?? 0), 0);
  const totalAmount = charges.reduce((s, c) => s + Number(c.amount ?? 0), 0);
  const avgKwhPrice = totalKwh ? totalAmount / totalKwh : 0;

  const fields: Field[] = [
    { name: "vehicle", label: "Véhicule électrique", type: "select", required: true,
      options: electricVehicles.map((v) => ({ value: v.id, label: `${v.registration} — ${v.brand} ${v.model}` })) },
    { name: "date", label: "Date", type: "date", required: true },
    { name: "kwh_recharged", label: "Énergie rechargée (kWh)", type: "number", required: true, min: 0, step: "0.01" },
    { name: "amount", label: "Coût total", type: "number", required: true, min: 0, step: "0.01" },
    { name: "kwh_price", label: "Prix du kWh", type: "number", min: 0, step: "0.01" },
    { name: "charge_type", label: "Type de recharge", type: "select",
      options: [
        { value: "ac_slow", label: "Recharge lente (AC)" },
        { value: "ac_fast", label: "Recharge accélérée (AC)" },
        { value: "dc_rapid", label: "Recharge rapide (DC)" },
        { value: "other", label: "Autre" },
      ] },
    { name: "charger", label: "Borne de recharge", type: "text" },
    { name: "duration_min", label: "Durée (min)", type: "number", min: 0 },
    { name: "soc_start_pct", label: "Charge initiale (%)", type: "number", min: 0 },
    { name: "soc_end_pct", label: "Charge finale (%)", type: "number", min: 0 },
    { name: "battery_capacity_kwh", label: "Capacité batterie (kWh)", type: "number", min: 0, step: "0.1" },
    { name: "range_estimate_km", label: "Autonomie estimée (km)", type: "number", min: 0 },
    { name: "mileage", label: "Km à la recharge", type: "number", min: 0 },
    ...(me?.has_company_scope
      ? [{ name: "subsidiary", label: "Filiale", type: "select" as const, required: true,
          options: (subs ?? []).map((s) => ({ value: s.id, label: s.name })) }]
      : []),
  ];

  function handleSubmit(values: Record<string, unknown>) {
    setError("");
    const opts = { onSuccess: () => setModal(null), onError: (e: unknown) => setError(apiError(e)) };
    if (modal?.mode === "edit") crud.update.mutate({ id: modal.row.id, body: values }, opts);
    else crud.create.mutate(values, opts);
  }

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <Mini icon={BatteryCharging} label="Énergie rechargée" value={formatNumber(totalKwh, "kWh")} tone="bg-emerald-500/10 text-emerald-600" />
        <Mini icon={Wallet} label="Coût électricité total" value={formatNumber(totalAmount)} tone="bg-brand-500/10 text-brand-600" />
        <Mini icon={Plug} label="Prix moyen / kWh" value={formatNumber(Math.round(avgKwhPrice))} tone="bg-violet-500/10 text-violet-600" />
      </div>

      <div className="flex">
        <Button
          className="ml-auto"
          disabled={electricVehicles.length === 0}
          title={electricVehicles.length === 0 ? "Aucun véhicule électrique dans la flotte" : undefined}
          onClick={() => { setError(""); setModal({ mode: "create" }); }}
        >
          <Plus className="h-4 w-4" /> Nouvelle recharge
        </Button>
      </div>

      <Card>
        <CardBody className="p-0">
          {isLoading ? (
            <div className="flex justify-center py-16"><Spinner className="h-7 w-7" /></div>
          ) : charges.length === 0 ? (
            <EmptyState
              title="Aucune recharge enregistrée"
              hint={electricVehicles.length === 0
                ? "Aucun véhicule électrique n'est encore déclaré dans la flotte."
                : "Enregistrez une recharge pour suivre la consommation électrique."}
            />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-faint">
                    <th className="px-5 py-3 font-medium">Date</th>
                    <th className="px-5 py-3 font-medium">Véhicule</th>
                    <th className="px-5 py-3 font-medium">kWh</th>
                    <th className="px-5 py-3 font-medium">Coût</th>
                    <th className="px-5 py-3 font-medium">Type</th>
                    <th className="px-5 py-3 font-medium">Charge</th>
                    <th className="px-5 py-3 font-medium">Borne</th>
                    <th className="px-5 py-3 font-medium text-right">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {charges.map((c) => (
                    <tr key={c.id} className="hover:bg-surface2">
                      <td className="px-5 py-3 text-muted">{formatDate(c.date)}</td>
                      <td className="px-5 py-3 font-medium text-ink">{c.vehicle_registration}</td>
                      <td className="px-5 py-3 text-muted">{formatNumber(c.kwh_recharged, "kWh")}</td>
                      <td className="px-5 py-3 text-muted">{formatNumber(c.amount)}</td>
                      <td className="px-5 py-3 text-muted">{c.charge_type_display}</td>
                      <td className="px-5 py-3 text-muted">
                        {c.soc_start_pct != null && c.soc_end_pct != null
                          ? `${c.soc_start_pct} → ${c.soc_end_pct} %`
                          : "—"}
                      </td>
                      <td className="px-5 py-3 text-muted">{c.charger || "—"}</td>
                      <td className="px-5 py-3">
                        <RowActions
                          label={`recharge ${c.vehicle_registration}`}
                          deleting={crud.remove.isPending}
                          onEdit={() => { setError(""); setModal({ mode: "edit", row: c }); }}
                          onDelete={() => crud.remove.mutate(c.id)}
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardBody>
      </Card>

      {modal && (
        <EntityForm
          open
          title={modal.mode === "edit" ? "Modifier la recharge" : "Nouvelle recharge"}
          fields={fields}
          initial={modal.mode === "edit" ? (modal.row as unknown as Record<string, unknown>) : {}}
          submitting={crud.create.isPending || crud.update.isPending}
          error={error}
          onClose={() => setModal(null)}
          onSubmit={handleSubmit}
        />
      )}
    </div>
  );
}
