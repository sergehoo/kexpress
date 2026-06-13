"use client";

import { useState } from "react";
import { Droplet, Fuel as FuelIcon, Plus, Wallet } from "lucide-react";

import { Button, Card, CardBody, EmptyState, Spinner } from "@/components/ui";
import { EntityForm, type Field } from "@/components/EntityForm";
import { RowActions } from "@/components/RowActions";
import { useFuel, useSubsidiaries, useVehicles } from "@/lib/queries";
import { useCrud } from "@/lib/crud";
import { useAuth } from "@/lib/auth";
import { apiError } from "@/lib/api";
import type { FuelLog } from "@/lib/types";
import { formatDate, formatNumber } from "@/lib/utils";

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

type ModalState = { mode: "create" } | { mode: "edit"; row: FuelLog } | null;

export default function FuelPage() {
  const { me } = useAuth();
  const [modal, setModal] = useState<ModalState>(null);
  const [error, setError] = useState("");
  const { data, isLoading } = useFuel();
  const { data: vehicles } = useVehicles();
  const { data: subs } = useSubsidiaries();
  const crud = useCrud("fuel", ["dashboard-stats"]);
  const logs = data?.results ?? [];

  const totalLiters = logs.reduce((s, l) => s + Number(l.liters ?? 0), 0);
  const totalAmount = logs.reduce((s, l) => s + Number(l.amount ?? 0), 0);
  const avgPrice = totalLiters ? totalAmount / totalLiters : 0;

  const fields: Field[] = [
    { name: "vehicle", label: "Véhicule", type: "select", required: true,
      options: (vehicles?.results ?? []).map((v) => ({ value: v.id, label: `${v.registration} — ${v.brand} ${v.model}` })) },
    { name: "date", label: "Date", type: "date", required: true },
    { name: "liters", label: "Litres", type: "number", required: true, min: 0, step: "0.01" },
    { name: "amount", label: "Montant", type: "number", required: true, min: 0, step: "0.01" },
    { name: "price_per_liter", label: "Prix / litre", type: "number", min: 0, step: "0.01" },
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
