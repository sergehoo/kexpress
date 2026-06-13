"use client";

import { useState } from "react";
import { AlertTriangle, Coins, Plus, ShieldCheck, Timer, Wrench } from "lucide-react";

import { Button, Card, CardBody, EmptyState, Select, Spinner } from "@/components/ui";
import { StatChips } from "@/components/StatChips";
import { StatusBadge } from "@/components/StatusBadge";
import { EntityForm, type Field } from "@/components/EntityForm";
import { RowActions } from "@/components/RowActions";
import {
  useBreakdownTypes,
  useEmployees,
  useMaintenance,
  useMaintenanceTypes,
  useSubsidiaries,
  useTrips,
  useVehicles,
} from "@/lib/queries";
import { useCrud } from "@/lib/crud";
import { useAuth } from "@/lib/auth";
import { apiError } from "@/lib/api";
import type { MaintenanceRecord } from "@/lib/types";
import { formatDate, formatNumber } from "@/lib/utils";

const STATUS_OPTS = [
  { value: "planned", label: "Planifiée" }, { value: "in_progress", label: "En cours" },
  { value: "completed", label: "Terminée" }, { value: "cancelled", label: "Annulée" },
];

const NATURE_OPTS = [
  { value: "preventive", label: "Maintenance préventive" },
  { value: "corrective", label: "Maintenance corrective" },
  { value: "urgent", label: "Réparation urgente" },
  { value: "periodic", label: "Entretien périodique" },
  { value: "other", label: "Autre" },
];

type ModalState = { mode: "create" } | { mode: "edit"; row: MaintenanceRecord } | null;

export default function MaintenancePage() {
  const { me } = useAuth();
  const [status, setStatus] = useState("");
  const [nature, setNature] = useState("");
  const [modal, setModal] = useState<ModalState>(null);
  const [error, setError] = useState("");

  const params: Record<string, string> = { page_size: "100" };
  if (status) params.status = status;
  if (nature) params.nature = nature;
  const { data, isLoading } = useMaintenance(params);
  const { data: vehicles } = useVehicles({ page_size: "100" });
  const { data: types } = useMaintenanceTypes();
  const { data: breakdowns } = useBreakdownTypes();
  const { data: subs } = useSubsidiaries();
  const { data: trips } = useTrips({ page_size: "50" });
  const { data: employees } = useEmployees({ page_size: "100" });
  const crud = useCrud("maintenance", ["dashboard-stats", "alerts"]);
  const records = data?.results ?? [];

  const totalCost = records.reduce((s, r) => s + Number(r.cost ?? 0), 0);
  const totalDowntime = records.reduce((s, r) => s + Number(r.downtime_hours ?? 0), 0);
  const breakdownCount = records.filter((r) => r.breakdown_type).length;

  const fields: Field[] = [
    { name: "vehicle", label: "Véhicule", type: "select", required: true,
      options: (vehicles?.results ?? []).map((v) => ({ value: v.id, label: `${v.registration} — ${v.brand} ${v.model}` })) },
    { name: "maintenance_type", label: "Type de maintenance", type: "select", required: true,
      options: (types ?? []).map((t) => ({ value: t.id, label: t.name })) },
    { name: "nature", label: "Nature", type: "select", options: NATURE_OPTS },
    { name: "breakdown_type", label: "Type de panne (si applicable)", type: "select",
      options: [{ value: "", label: "—" }, ...(breakdowns ?? []).map((b) => ({ value: b.id, label: b.name }))] },
    { name: "trip", label: "Course liée (imputation filiale auto)", type: "select",
      options: [{ value: "", label: "—" }, ...(trips?.results ?? []).map((t) => ({
        value: t.id, label: `${t.destination} · ${t.vehicle_registration} (${t.subsidiary_name})`,
      }))] },
    { name: "status", label: "Statut", type: "select", options: STATUS_OPTS },
    { name: "declared_date", label: "Date de déclaration", type: "date" },
    { name: "scheduled_date", label: "Date prévue", type: "date" },
    { name: "performed_date", label: "Date réalisée", type: "date" },
    { name: "downtime_start", label: "Immobilisation — début", type: "datetime" },
    { name: "downtime_end", label: "Immobilisation — fin", type: "datetime" },
    { name: "mileage", label: "Km à l'intervention", type: "number", min: 0 },
    { name: "labor_cost", label: "Coût main-d'œuvre", type: "number", min: 0, step: "0.01" },
    { name: "parts_cost", label: "Coût pièces", type: "number", min: 0, step: "0.01" },
    { name: "cost", label: "Coût total (auto si vide)", type: "number", min: 0, step: "0.01" },
    { name: "provider", label: "Fournisseur / garage" },
    { name: "validated_by", label: "Responsable de validation", type: "select",
      options: [{ value: "", label: "—" }, ...(employees?.results ?? []).map((e) => ({
        value: e.id, label: e.full_name || e.email,
      }))] },
    ...(me?.has_company_scope
      ? [{ name: "subsidiary", label: "Filiale (ignorée si course liée)", type: "select" as const,
          options: (subs ?? []).map((s) => ({ value: s.id, label: s.name })) }]
      : []),
    { name: "notes", label: "Description", type: "textarea" },
  ];

  function handleSubmit(values: Record<string, unknown>) {
    setError("");
    // Champs vides "" → null pour les FK optionnelles
    for (const k of ["breakdown_type", "trip", "validated_by"]) {
      if (values[k] === "") values[k] = null;
    }
    if (values.cost === "" || values.cost == null) delete values.cost;
    const opts = { onSuccess: () => setModal(null), onError: (e: unknown) => setError(apiError(e)) };
    if (modal?.mode === "edit") crud.update.mutate({ id: modal.row.id, body: values }, opts);
    else crud.create.mutate(values, opts);
  }

  const preventiveCount = records.filter((r) => ["preventive", "periodic"].includes(r.nature)).length;
  const correctiveCount = records.filter((r) => ["corrective", "urgent"].includes(r.nature)).length;

  return (
    <div className="space-y-5">
      <StatChips
        stats={[
          { label: "Interventions", value: records.length, icon: Wrench, tone: "bg-brand-500/10 text-brand-600" },
          { label: "Pannes déclarées", value: breakdownCount, icon: AlertTriangle, tone: "bg-rose-500/10 text-rose-600" },
          { label: "Préventive / corrective", value: `${preventiveCount} / ${correctiveCount}`, icon: ShieldCheck, tone: "bg-emerald-500/10 text-emerald-600" },
          { label: "Indisponibilité", value: `${formatNumber(totalDowntime)} h`, icon: Timer, tone: "bg-violet-500/10 text-violet-600" },
          { label: "Coût total", value: formatNumber(totalCost), icon: Coins, tone: "bg-amber-500/10 text-amber-600", sub: "XOF" },
        ]}
      />

      <div className="flex flex-wrap items-center gap-3">
        <Select value={status} onChange={(e) => setStatus(e.target.value)} className="sm:w-44">
          <option value="">Tous les statuts</option>
          {STATUS_OPTS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </Select>
        <Select value={nature} onChange={(e) => setNature(e.target.value)} className="sm:w-52">
          <option value="">Toutes les natures</option>
          {NATURE_OPTS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </Select>
        <Button className="ml-auto" onClick={() => { setError(""); setModal({ mode: "create" }); }}>
          <Plus className="h-4 w-4" /> Nouvelle
        </Button>
      </div>

      <Card>
        <CardBody className="p-0">
          {isLoading ? (
            <div className="flex justify-center py-16"><Spinner className="h-7 w-7" /></div>
          ) : records.length === 0 ? (
            <EmptyState title="Aucune intervention" />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-faint">
                    <th className="px-4 py-3 font-medium">Véhicule</th>
                    <th className="px-4 py-3 font-medium">Type</th>
                    <th className="px-4 py-3 font-medium">Nature</th>
                    <th className="px-4 py-3 font-medium">Panne</th>
                    <th className="px-4 py-3 font-medium">Filiale</th>
                    <th className="px-4 py-3 font-medium">Statut</th>
                    <th className="px-4 py-3 font-medium">Déclarée</th>
                    <th className="px-4 py-3 font-medium">Immob.</th>
                    <th className="px-4 py-3 font-medium">MO / Pièces</th>
                    <th className="px-4 py-3 font-medium">Coût total</th>
                    <th className="px-4 py-3 font-medium text-right">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {records.map((r) => (
                    <tr key={r.id} className="hover:bg-surface2">
                      <td className="px-4 py-3 font-medium text-ink">
                        {r.vehicle_registration}
                        {r.trip_destination && (
                          <p className="text-[10px] font-normal text-faint">course : {r.trip_destination}</p>
                        )}
                      </td>
                      <td className="px-4 py-3 text-muted">{r.type_name}</td>
                      <td className="px-4 py-3 text-muted">{r.nature_display}</td>
                      <td className="px-4 py-3">
                        {r.breakdown_name ? (
                          <span className="rounded-md bg-rose-500/10 px-2 py-0.5 text-[11px] font-medium text-rose-600">{r.breakdown_name}</span>
                        ) : <span className="text-faint">—</span>}
                      </td>
                      <td className="px-4 py-3 text-muted">{r.subsidiary_name}</td>
                      <td className="px-4 py-3"><StatusBadge code={r.status} label={r.status_display} /></td>
                      <td className="px-4 py-3 text-muted">{formatDate(r.declared_date ?? r.scheduled_date)}</td>
                      <td className="px-4 py-3 text-muted">{r.downtime_hours != null ? `${formatNumber(r.downtime_hours)} h` : "—"}</td>
                      <td className="px-4 py-3 text-muted">
                        {r.labor_cost || r.parts_cost
                          ? `${formatNumber(r.labor_cost ?? 0)} / ${formatNumber(r.parts_cost ?? 0)}`
                          : "—"}
                      </td>
                      <td className="px-4 py-3 font-semibold text-ink">{formatNumber(r.cost)}</td>
                      <td className="px-4 py-3">
                        <RowActions
                          label={`${r.type_name} — ${r.vehicle_registration}`}
                          deleting={crud.remove.isPending}
                          onEdit={() => { setError(""); setModal({ mode: "edit", row: r }); }}
                          onDelete={() => crud.remove.mutate(r.id)}
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
          title={modal.mode === "edit" ? "Modifier l'intervention" : "Nouvelle intervention"}
          fields={fields}
          initial={modal.mode === "edit" ? (modal.row as unknown as Record<string, unknown>) : { status: "planned", nature: "corrective" }}
          submitting={crud.create.isPending || crud.update.isPending}
          error={error}
          onClose={() => setModal(null)}
          onSubmit={handleSubmit}
        />
      )}
    </div>
  );
}
