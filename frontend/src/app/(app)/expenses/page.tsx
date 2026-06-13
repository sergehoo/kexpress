"use client";

import { useState } from "react";
import { Car, Coins, Plus, Receipt, TrendingUp, Wallet } from "lucide-react";

import { Button, Card, CardBody, EmptyState, Select, Spinner } from "@/components/ui";
import { Modal } from "@/components/Modal";
import { EntityForm, type Field } from "@/components/EntityForm";
import { RowActions } from "@/components/RowActions";
import { StatChips } from "@/components/StatChips";
import { useExpenses, useSubsidiaries, useTrips, useVehicles } from "@/lib/queries";
import { useCrud } from "@/lib/crud";
import { useAuth } from "@/lib/auth";
import { apiError } from "@/lib/api";
import type { Expense } from "@/lib/types";
import { formatDate, formatNumber } from "@/lib/utils";

const CATEGORIES = [
  { value: "fuel", label: "Carburant" }, { value: "maintenance", label: "Maintenance" },
  { value: "insurance", label: "Assurance" }, { value: "toll", label: "Péage" },
  { value: "fine", label: "Amende" }, { value: "unexpected", label: "Imprévu" },
  { value: "other", label: "Autre" },
];

type ModalState =
  | { mode: "create" }
  | { mode: "edit"; row: Expense }
  | { mode: "detail"; row: Expense }
  | null;

export default function ExpensesPage() {
  const { me } = useAuth();
  const [category, setCategory] = useState("");
  const [modal, setModal] = useState<ModalState>(null);
  const [error, setError] = useState("");

  const params: Record<string, string> = { page_size: "100" };
  if (category) params.category = category;
  const { data, isLoading } = useExpenses(params);
  const { data: vehicles } = useVehicles({ page_size: "100" });
  const { data: subs } = useSubsidiaries();
  const { data: trips } = useTrips({ page_size: "50" });
  const crud = useCrud("expenses", ["dashboard-stats"]);
  const expenses = data?.results ?? [];

  // Statistiques de la liste affichée
  const total = expenses.reduce((s, e) => s + Number(e.amount ?? 0), 0);
  const avg = expenses.length ? total / expenses.length : 0;
  const byCat = expenses.reduce<Record<string, number>>((acc, e) => {
    acc[e.category_display] = (acc[e.category_display] ?? 0) + Number(e.amount ?? 0);
    return acc;
  }, {});
  const topCat = Object.entries(byCat).sort((a, b) => b[1] - a[1])[0];
  const withVehicle = expenses.filter((e) => e.vehicle).length;

  const fields: Field[] = [
    { name: "label", label: "Libellé", required: true, full: true },
    { name: "category", label: "Catégorie", type: "select", required: true, options: CATEGORIES },
    { name: "amount", label: "Montant", type: "number", required: true, min: 0, step: "0.01" },
    { name: "date", label: "Date", type: "date", required: true },
    { name: "vehicle", label: "Véhicule (optionnel)", type: "select",
      options: [{ value: "", label: "—" }, ...(vehicles?.results ?? []).map((v) => ({ value: v.id, label: v.registration }))] },
    { name: "trip", label: "Course liée (imputation filiale auto)", type: "select",
      options: [{ value: "", label: "—" }, ...(trips?.results ?? []).map((t) => ({
        value: t.id, label: `${t.destination} · ${t.vehicle_registration} (${t.subsidiary_name})`,
      }))] },
    ...(me?.has_company_scope
      ? [{ name: "subsidiary", label: "Filiale (ignorée si course liée)", type: "select" as const,
          options: (subs ?? []).map((s) => ({ value: s.id, label: s.name })) }]
      : []),
  ];

  function handleSubmit(values: Record<string, unknown>) {
    setError("");
    for (const k of ["vehicle", "trip"]) if (values[k] === "") values[k] = null;
    const opts = { onSuccess: () => setModal(null), onError: (e: unknown) => setError(apiError(e)) };
    if (modal?.mode === "edit") crud.update.mutate({ id: modal.row.id, body: values }, opts);
    else crud.create.mutate(values, opts);
  }

  return (
    <div className="space-y-5">
      <StatChips
        stats={[
          { label: "Dépenses affichées", value: expenses.length, icon: Receipt, tone: "bg-brand-500/10 text-brand-600" },
          { label: "Montant total", value: formatNumber(total), icon: Wallet, tone: "bg-amber-500/10 text-amber-600", sub: "XOF" },
          { label: "Dépense moyenne", value: formatNumber(Math.round(avg)), icon: TrendingUp, tone: "bg-violet-500/10 text-violet-600", sub: "XOF" },
          { label: "Top catégorie", value: topCat ? topCat[0] : "—", icon: Coins, tone: "bg-sky-500/10 text-sky-600", sub: topCat ? `${formatNumber(topCat[1])} XOF` : undefined },
          { label: "Liées à un véhicule", value: `${withVehicle}/${expenses.length}`, icon: Car, tone: "bg-emerald-500/10 text-emerald-600" },
        ]}
      />

      <div className="flex items-center gap-3">
        <Select value={category} onChange={(e) => setCategory(e.target.value)} className="sm:w-52">
          <option value="">Toutes catégories</option>
          {CATEGORIES.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
        </Select>
        <Button className="ml-auto" onClick={() => { setError(""); setModal({ mode: "create" }); }}>
          <Plus className="h-4 w-4" /> Nouvelle
        </Button>
      </div>

      <Card>
        <CardBody className="p-0">
          {isLoading ? (
            <div className="flex justify-center py-16"><Spinner className="h-7 w-7" /></div>
          ) : expenses.length === 0 ? (
            <EmptyState title="Aucune dépense" />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-faint">
                    <th className="px-5 py-3 font-medium">Date</th>
                    <th className="px-5 py-3 font-medium">Libellé</th>
                    <th className="px-5 py-3 font-medium">Catégorie</th>
                    <th className="px-5 py-3 font-medium">Véhicule</th>
                    <th className="px-5 py-3 font-medium">Filiale</th>
                    <th className="px-5 py-3 font-medium text-right">Montant</th>
                    <th className="px-5 py-3 font-medium text-right">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {expenses.map((e) => (
                    <tr key={e.id} className="cursor-pointer hover:bg-surface2" onClick={() => setModal({ mode: "detail", row: e })}>
                      <td className="px-5 py-3 text-muted">{formatDate(e.date)}</td>
                      <td className="px-5 py-3 font-medium text-ink">{e.label}</td>
                      <td className="px-5 py-3"><span className="rounded-full bg-surface2 px-2.5 py-0.5 text-xs text-muted">{e.category_display}</span></td>
                      <td className="px-5 py-3 text-muted">{e.vehicle_registration ?? "—"}</td>
                      <td className="px-5 py-3 text-muted">{e.subsidiary_name}</td>
                      <td className="px-5 py-3 text-right font-medium text-ink">{formatNumber(e.amount)}</td>
                      <td className="px-5 py-3" onClick={(ev) => ev.stopPropagation()}>
                        <RowActions
                          label={e.label}
                          deleting={crud.remove.isPending}
                          onEdit={() => { setError(""); setModal({ mode: "edit", row: e }); }}
                          onDelete={() => crud.remove.mutate(e.id)}
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

      {/* Détail d'une dépense */}
      {modal?.mode === "detail" && (
        <Modal open title="Détail de la dépense" onClose={() => setModal(null)}>
          <div className="space-y-2 text-sm">
            <p className="text-base font-semibold text-ink">{modal.row.label}</p>
            <div className="grid grid-cols-2 gap-2 rounded-lg bg-surface2 p-3 text-xs text-muted">
              <span>Montant : <b className="text-ink">{formatNumber(modal.row.amount)} XOF</b></span>
              <span>Date : <b className="text-ink">{formatDate(modal.row.date)}</b></span>
              <span>Catégorie : <b className="text-ink">{modal.row.category_display}</b></span>
              <span>Filiale : <b className="text-ink">{modal.row.subsidiary_name}</b></span>
              <span>Véhicule : <b className="text-ink">{modal.row.vehicle_registration ?? "—"}</b></span>
              <span>Enregistrée le : <b className="text-ink">{formatDate(modal.row.created_at, true)}</b></span>
            </div>
          </div>
          <div className="flex justify-end gap-2 pt-3">
            <Button variant="secondary" onClick={() => setModal(null)}>Fermer</Button>
            <Button onClick={() => setModal({ mode: "edit", row: modal.row })}>Modifier</Button>
          </div>
        </Modal>
      )}

      {(modal?.mode === "create" || modal?.mode === "edit") && (
        <EntityForm
          open
          title={modal.mode === "edit" ? "Modifier la dépense" : "Nouvelle dépense"}
          fields={fields}
          initial={modal.mode === "edit" ? (modal.row as unknown as Record<string, unknown>) : { category: "other" }}
          submitting={crud.create.isPending || crud.update.isPending}
          error={error}
          onClose={() => setModal(null)}
          onSubmit={handleSubmit}
        />
      )}
    </div>
  );
}
