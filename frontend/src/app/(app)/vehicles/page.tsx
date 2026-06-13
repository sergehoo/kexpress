"use client";

import { useState } from "react";
import Link from "next/link";
import { Car, CheckCircle2, Plus, Route, Search, ShieldCheck, Wrench } from "lucide-react";

import { StatChips } from "@/components/StatChips";

import { Button, Card, CardBody, EmptyState, Input, Select, Spinner } from "@/components/ui";
import { StatusBadge } from "@/components/StatusBadge";
import { EntityForm, type Field } from "@/components/EntityForm";
import { RowActions } from "@/components/RowActions";
import { useSubsidiaries, useVehicles } from "@/lib/queries";
import { useCrud } from "@/lib/crud";
import { useAuth } from "@/lib/auth";
import { apiError } from "@/lib/api";
import type { Vehicle } from "@/lib/types";
import { formatNumber } from "@/lib/utils";

const STATUS_OPTIONS = [
  { value: "", label: "Tous les statuts" },
  { value: "available", label: "Disponible" },
  { value: "reserved", label: "Réservé" },
  { value: "on_trip", label: "En course" },
  { value: "maintenance", label: "En maintenance" },
  { value: "out_of_service", label: "Hors service" },
];
const TYPE_OPTS = [
  { value: "sedan", label: "Berline" }, { value: "suv", label: "SUV / 4x4" },
  { value: "pickup", label: "Pick-up" }, { value: "van", label: "Utilitaire" },
  { value: "bus", label: "Bus / Minibus" }, { value: "truck", label: "Camion" },
  { value: "motorcycle", label: "Moto" }, { value: "other", label: "Autre" },
];
const FUEL_OPTS = [
  { value: "gasoline", label: "Essence" }, { value: "diesel", label: "Diesel" },
  { value: "hybrid", label: "Hybride" }, { value: "electric", label: "Électrique" },
  { value: "lpg", label: "GPL" }, { value: "other", label: "Autre" },
];
const STATUS_OPTS = STATUS_OPTIONS.slice(1);

type ModalState = { mode: "create" } | { mode: "edit"; row: Vehicle } | null;

export default function VehiclesPage() {
  const { me } = useAuth();
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [modal, setModal] = useState<ModalState>(null);
  const [error, setError] = useState("");

  const params: Record<string, string> = {};
  if (search) params.search = search;
  if (status) params.status = status;
  const { data, isLoading, isError } = useVehicles(params);
  const { data: subs } = useSubsidiaries();
  const crud = useCrud("vehicles", ["dashboard-stats", "fleet-positions"]);
  const vehicles = data?.results ?? [];

  const fields: Field[] = [
    { name: "registration", label: "Immatriculation", required: true },
    { name: "vehicle_type", label: "Type", type: "select", options: TYPE_OPTS, required: true },
    { name: "brand", label: "Marque", required: true },
    { name: "model", label: "Modèle", required: true },
    { name: "capacity", label: "Capacité (places)", type: "number", min: 1 },
    { name: "mileage", label: "Kilométrage", type: "number", min: 0 },
    { name: "revision_interval_km", label: "Intervalle de révision (km)", type: "number", min: 1000 },
    { name: "fuel_type", label: "Carburant", type: "select", options: FUEL_OPTS },
    { name: "status", label: "État", type: "select", options: STATUS_OPTS },
    ...(me?.has_company_scope
      ? [{ name: "subsidiary", label: "Filiale", type: "select" as const, required: true,
          options: (subs ?? []).map((s) => ({ value: s.id, label: s.name })) }]
      : []),
    { name: "notes", label: "Notes", type: "textarea" },
  ];

  function handleSubmit(values: Record<string, unknown>) {
    setError("");
    const opts = {
      onSuccess: () => setModal(null),
      onError: (e: unknown) => setError(apiError(e)),
    };
    if (modal?.mode === "edit") crud.update.mutate({ id: modal.row.id, body: values }, opts);
    else crud.create.mutate(values, opts);
  }

  const compliant = vehicles.filter((v) => v.compliance?.compliant).length;

  return (
    <div className="space-y-5">
      <StatChips
        stats={[
          { label: "Véhicules", value: vehicles.length, icon: Car, tone: "bg-brand-500/10 text-brand-600" },
          { label: "Disponibles", value: vehicles.filter((v) => v.status === "available").length, icon: CheckCircle2, tone: "bg-emerald-500/10 text-emerald-600" },
          { label: "En course", value: vehicles.filter((v) => v.status === "on_trip").length, icon: Route, tone: "bg-sky-500/10 text-sky-600" },
          { label: "En maintenance", value: vehicles.filter((v) => ["maintenance", "out_of_service"].includes(v.status)).length, icon: Wrench, tone: "bg-amber-500/10 text-amber-600" },
          {
            label: "Conformes",
            value: `${compliant}/${vehicles.length}`,
            icon: ShieldCheck,
            tone: compliant === vehicles.length ? "bg-emerald-500/10 text-emerald-600" : "bg-rose-500/10 text-rose-600",
            sub: vehicles.length ? `${Math.round((compliant / vehicles.length) * 100)}% de conformité` : undefined,
          },
        ]}
      />

      <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-faint" />
          <Input placeholder="Rechercher (immatriculation, marque, modèle)…" value={search} onChange={(e) => setSearch(e.target.value)} className="pl-9" />
        </div>
        <Select value={status} onChange={(e) => setStatus(e.target.value)} className="sm:w-52">
          {STATUS_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </Select>
        <Button onClick={() => { setError(""); setModal({ mode: "create" }); }}>
          <Plus className="h-4 w-4" /> Nouveau
        </Button>
      </div>

      <Card>
        <CardBody className="p-0">
          {isLoading ? (
            <div className="flex justify-center py-16"><Spinner className="h-7 w-7" /></div>
          ) : isError ? (
            <EmptyState title="Erreur de chargement" />
          ) : vehicles.length === 0 ? (
            <EmptyState title="Aucun véhicule" hint="Ajoutez votre premier véhicule." />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-faint">
                    <th className="px-5 py-3 font-medium">Immatriculation</th>
                    <th className="px-5 py-3 font-medium">Véhicule</th>
                    <th className="px-5 py-3 font-medium">Type</th>
                    <th className="px-5 py-3 font-medium">Filiale</th>
                    <th className="px-5 py-3 font-medium">Km</th>
                    <th className="px-5 py-3 font-medium">Statut</th>
                    <th className="px-5 py-3 font-medium">Conformité</th>
                    <th className="px-5 py-3 font-medium text-right">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {vehicles.map((v) => (
                    <tr key={v.id} className="hover:bg-surface2">
                      <td className="px-5 py-3 font-medium text-ink">
                        <Link href={`/vehicles/${v.id}`} className="hover:text-brand-600 hover:underline">
                          {v.registration}
                        </Link>
                      </td>
                      <td className="px-5 py-3 text-muted">{v.brand} {v.model}<span className="ml-2 text-xs text-faint">{v.capacity} pl.</span></td>
                      <td className="px-5 py-3 text-muted">{v.vehicle_type_display}</td>
                      <td className="px-5 py-3 text-muted">{v.subsidiary_name}</td>
                      <td className="px-5 py-3 text-muted">{formatNumber(v.mileage, "km")}</td>
                      <td className="px-5 py-3"><StatusBadge code={v.status} label={v.status_display} /></td>
                      <td className="px-5 py-3">
                        {v.compliance?.compliant ? (
                          <div>
                            <span className="inline-flex items-center rounded-full bg-emerald-500/10 px-2 py-0.5 text-[11px] font-medium text-emerald-600">
                              ✓ Conforme
                            </span>
                            <p className="mt-0.5 text-[10px] text-faint">
                              {v.compliance.insurance_days_left != null && `Assurance J-${v.compliance.insurance_days_left}`}
                              {v.compliance.revision_remaining_km > 0 && v.compliance.revision_remaining_km <= 2000 &&
                                ` · révision dans ${v.compliance.revision_remaining_km} km`}
                            </p>
                          </div>
                        ) : (
                          <div>
                            <span
                              className="inline-flex items-center rounded-full bg-rose-500/10 px-2 py-0.5 text-[11px] font-medium text-rose-600"
                              title={(v.compliance?.issues ?? []).map((i) => i.label).join(" · ")}
                            >
                              ✕ Non conforme
                            </span>
                            <p className="mt-0.5 max-w-[16rem] truncate text-[10px] text-rose-500/80">
                              {(v.compliance?.issues ?? []).map((i) => i.label).join(" · ")}
                            </p>
                          </div>
                        )}
                      </td>
                      <td className="px-5 py-3">
                        <RowActions
                          label={v.registration}
                          deleting={crud.remove.isPending}
                          onEdit={() => { setError(""); setModal({ mode: "edit", row: v }); }}
                          onDelete={() => crud.remove.mutate(v.id)}
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
          title={modal.mode === "edit" ? "Modifier le véhicule" : "Nouveau véhicule"}
          fields={fields}
          initial={modal.mode === "edit" ? (modal.row as unknown as Record<string, unknown>) : { status: "available", fuel_type: "diesel", vehicle_type: "sedan", capacity: 5 }}
          submitting={crud.create.isPending || crud.update.isPending}
          error={error}
          onClose={() => setModal(null)}
          onSubmit={handleSubmit}
        />
      )}
    </div>
  );
}
