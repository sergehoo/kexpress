"use client";

import { useState } from "react";
import Link from "next/link";
import { AlertTriangle, CheckCircle2, Plus, Search, Star, Users } from "lucide-react";

import { Button, Card, CardBody, EmptyState, Input, Select, Spinner } from "@/components/ui";
import { StatChips } from "@/components/StatChips";
import { EntityForm, type Field } from "@/components/EntityForm";
import { RowActions } from "@/components/RowActions";
import { useDrivers, useSubsidiaries } from "@/lib/queries";
import { useCrud } from "@/lib/crud";
import { useAuth } from "@/lib/auth";
import { apiError } from "@/lib/api";
import type { Driver } from "@/lib/types";
import { formatDate } from "@/lib/utils";

type ModalState = { mode: "create" } | { mode: "edit"; row: Driver } | null;

export default function DriversPage() {
  const { me } = useAuth();
  const [search, setSearch] = useState("");
  const [avail, setAvail] = useState("");
  const [modal, setModal] = useState<ModalState>(null);
  const [error, setError] = useState("");

  const params: Record<string, string> = {};
  if (search) params.search = search;
  if (avail) params.is_available = avail;
  const { data, isLoading } = useDrivers(params);
  const { data: subs } = useSubsidiaries();
  const crud = useCrud("drivers");
  const drivers = data?.results ?? [];

  const fields: Field[] = [
    { name: "first_name", label: "Prénom", required: true },
    { name: "last_name", label: "Nom", required: true },
    { name: "phone", label: "Téléphone" },
    { name: "email", label: "Email", type: "email" },
    { name: "license_number", label: "N° de permis" },
    { name: "license_category", label: "Catégorie permis" },
    { name: "license_expiry", label: "Expiration permis", type: "date" },
    { name: "is_available", label: "Disponible", type: "checkbox" },
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

  const expiringSoon = drivers.filter((d) => {
    if (!d.license_expiry) return false;
    const days = (new Date(d.license_expiry).getTime() - Date.now()) / 86_400_000;
    return days <= 30;
  }).length;
  const ratings = drivers.map((d) => Number(d.rating)).filter((r) => r > 0);
  const avgRating = ratings.length ? (ratings.reduce((a, b) => a + b, 0) / ratings.length).toFixed(1) : "—";

  return (
    <div className="space-y-5">
      <StatChips
        stats={[
          { label: "Chauffeurs", value: drivers.length, icon: Users, tone: "bg-brand-500/10 text-brand-600" },
          { label: "Disponibles", value: drivers.filter((d) => d.is_available).length, icon: CheckCircle2, tone: "bg-emerald-500/10 text-emerald-600" },
          { label: "Indisponibles", value: drivers.filter((d) => !d.is_available).length, icon: Users, tone: "bg-slate-500/10 text-slate-500" },
          { label: "Permis ≤ 30 j / expirés", value: expiringSoon, icon: AlertTriangle, tone: expiringSoon ? "bg-rose-500/10 text-rose-600" : "bg-emerald-500/10 text-emerald-600" },
          { label: "Note moyenne", value: avgRating, icon: Star, tone: "bg-amber-500/10 text-amber-600" },
        ]}
      />

      <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-faint" />
          <Input placeholder="Rechercher un chauffeur…" value={search} onChange={(e) => setSearch(e.target.value)} className="pl-9" />
        </div>
        <Select value={avail} onChange={(e) => setAvail(e.target.value)} className="sm:w-44">
          <option value="">Tous</option>
          <option value="true">Disponibles</option>
          <option value="false">Indisponibles</option>
        </Select>
        <Button onClick={() => { setError(""); setModal({ mode: "create" }); }}>
          <Plus className="h-4 w-4" /> Nouveau
        </Button>
      </div>

      <Card>
        <CardBody className="p-0">
          {isLoading ? (
            <div className="flex justify-center py-16"><Spinner className="h-7 w-7" /></div>
          ) : drivers.length === 0 ? (
            <EmptyState title="Aucun chauffeur" />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-faint">
                    <th className="px-5 py-3 font-medium">Chauffeur</th>
                    <th className="px-5 py-3 font-medium">Téléphone</th>
                    <th className="px-5 py-3 font-medium">Permis</th>
                    <th className="px-5 py-3 font-medium">Expiration</th>
                    <th className="px-5 py-3 font-medium">Note</th>
                    <th className="px-5 py-3 font-medium">Disponibilité</th>
                    <th className="px-5 py-3 font-medium text-right">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {drivers.map((d) => (
                    <tr key={d.id} className="hover:bg-surface2">
                      <td className="px-5 py-3 font-medium text-ink">
                        <Link href={`/drivers/${d.id}`} className="hover:text-brand-600 hover:underline">
                          {d.full_name}
                        </Link>
                      </td>
                      <td className="px-5 py-3 text-muted">{d.phone || "—"}</td>
                      <td className="px-5 py-3 text-muted">{d.license_category || "—"}</td>
                      <td className="px-5 py-3 text-muted">{formatDate(d.license_expiry)}</td>
                      <td className="px-5 py-3 text-muted">
                        <span className="inline-flex items-center gap-1"><Star className="h-3.5 w-3.5 text-amber-500" />{d.rating ?? "—"}</span>
                      </td>
                      <td className="px-5 py-3">
                        <span className={"inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset " + (d.is_available ? "bg-emerald-500/10 text-emerald-600 ring-emerald-500/20" : "bg-slate-500/10 text-slate-500 ring-slate-500/20")}>
                          {d.is_available ? "Disponible" : "Indisponible"}
                        </span>
                      </td>
                      <td className="px-5 py-3">
                        <RowActions
                          label={d.full_name}
                          deleting={crud.remove.isPending}
                          onEdit={() => { setError(""); setModal({ mode: "edit", row: d }); }}
                          onDelete={() => crud.remove.mutate(d.id)}
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
          title={modal.mode === "edit" ? "Modifier le chauffeur" : "Nouveau chauffeur"}
          fields={fields}
          initial={modal.mode === "edit" ? (modal.row as unknown as Record<string, unknown>) : { is_available: true }}
          submitting={crud.create.isPending || crud.update.isPending}
          error={error}
          onClose={() => setModal(null)}
          onSubmit={handleSubmit}
        />
      )}
    </div>
  );
}
