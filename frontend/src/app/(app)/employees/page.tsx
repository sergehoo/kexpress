"use client";

import { useState } from "react";
import { Plus, Search } from "lucide-react";

import { Button, Card, CardBody, EmptyState, Input, Select, Spinner } from "@/components/ui";
import { EntityForm, type Field } from "@/components/EntityForm";
import { RowActions } from "@/components/RowActions";
import { useEmployees, useSubsidiaries } from "@/lib/queries";
import { useCrud } from "@/lib/crud";
import { useAuth } from "@/lib/auth";
import { apiError } from "@/lib/api";
import type { Employee } from "@/lib/types";

const ROLES = [
  { value: "company_admin", label: "Admin entreprise" },
  { value: "subsidiary_admin", label: "Admin filiale" },
  { value: "fleet_manager", label: "Gestionnaire flotte" },
  { value: "department_manager", label: "Responsable service" },
  { value: "requester", label: "Employé demandeur" },
  { value: "driver", label: "Chauffeur" },
  { value: "finance", label: "Finance" },
  { value: "auditor", label: "Auditeur" },
];

type ModalState = { mode: "create" } | { mode: "edit"; row: Employee } | null;

export default function EmployeesPage() {
  const { me } = useAuth();
  const [search, setSearch] = useState("");
  const [role, setRole] = useState("");
  const [modal, setModal] = useState<ModalState>(null);
  const [error, setError] = useState("");

  const params: Record<string, string> = {};
  if (search) params.search = search;
  if (role) params.role = role;
  const { data, isLoading } = useEmployees(params);
  const { data: subs } = useSubsidiaries();
  const crud = useCrud("employees");
  const employees = data?.results ?? [];
  const canManage = me?.has_company_scope || me?.role === "subsidiary_admin";

  const initials = (name: string) => name.split(" ").map((p) => p[0]).slice(0, 2).join("").toUpperCase();

  const fields: Field[] = [
    { name: "email", label: "Email", type: "email", required: true, full: true },
    { name: "first_name", label: "Prénom" },
    { name: "last_name", label: "Nom" },
    { name: "phone", label: "Téléphone" },
    { name: "role", label: "Rôle", type: "select", required: true, options: ROLES },
    ...(me?.has_company_scope
      ? [{ name: "subsidiary", label: "Filiale", type: "select" as const,
          options: (subs ?? []).map((s) => ({ value: s.id, label: s.name })) }]
      : []),
    { name: "password", label: "Mot de passe (défaut: demo1234)", type: "text" },
    { name: "is_active", label: "Actif", type: "checkbox" },
  ];

  function handleSubmit(values: Record<string, unknown>) {
    setError("");
    const opts = { onSuccess: () => setModal(null), onError: (e: unknown) => setError(apiError(e)) };
    if (modal?.mode === "edit") crud.update.mutate({ id: modal.row.id, body: values }, opts);
    else crud.create.mutate(values, opts);
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-faint" />
          <Input placeholder="Rechercher un employé…" value={search} onChange={(e) => setSearch(e.target.value)} className="pl-9" />
        </div>
        <Select value={role} onChange={(e) => setRole(e.target.value)} className="sm:w-52">
          <option value="">Tous les rôles</option>
          {ROLES.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
        </Select>
        {canManage && (
          <Button onClick={() => { setError(""); setModal({ mode: "create" }); }}>
            <Plus className="h-4 w-4" /> Nouveau
          </Button>
        )}
      </div>

      <Card>
        <CardBody className="p-0">
          {isLoading ? (
            <div className="flex justify-center py-16"><Spinner className="h-7 w-7" /></div>
          ) : employees.length === 0 ? (
            <EmptyState title="Aucun employé" />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-faint">
                    <th className="px-5 py-3 font-medium">Employé</th>
                    <th className="px-5 py-3 font-medium">Rôle</th>
                    <th className="px-5 py-3 font-medium">Filiale</th>
                    <th className="px-5 py-3 font-medium">Statut</th>
                    {canManage && <th className="px-5 py-3 font-medium text-right">Actions</th>}
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {employees.map((e) => (
                    <tr key={e.id} className="hover:bg-surface2">
                      <td className="px-5 py-3">
                        <div className="flex items-center gap-3">
                          <span className="flex h-8 w-8 items-center justify-center rounded-full bg-brand-500/10 text-xs font-semibold text-brand-600">{initials(e.full_name || e.email)}</span>
                          <div className="min-w-0"><p className="truncate font-medium text-ink">{e.full_name || "—"}</p><p className="truncate text-xs text-faint">{e.email}</p></div>
                        </div>
                      </td>
                      <td className="px-5 py-3 text-muted">{e.role_display}</td>
                      <td className="px-5 py-3 text-muted">{e.subsidiary_name ?? "Entreprise"}</td>
                      <td className="px-5 py-3">
                        <span className={"inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset " + (e.is_active ? "bg-emerald-500/10 text-emerald-600 ring-emerald-500/20" : "bg-slate-500/10 text-slate-500 ring-slate-500/20")}>
                          {e.is_active ? "Actif" : "Inactif"}
                        </span>
                      </td>
                      {canManage && (
                        <td className="px-5 py-3">
                          <RowActions
                            label={e.full_name || e.email}
                            deleting={crud.remove.isPending}
                            onEdit={() => { setError(""); setModal({ mode: "edit", row: e }); }}
                            onDelete={() => crud.remove.mutate(e.id)}
                          />
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

      {modal && (
        <EntityForm
          open
          title={modal.mode === "edit" ? "Modifier l'employé" : "Nouvel employé"}
          fields={fields}
          initial={modal.mode === "edit" ? (modal.row as unknown as Record<string, unknown>) : { is_active: true, role: "requester" }}
          submitting={crud.create.isPending || crud.update.isPending}
          error={error}
          onClose={() => setModal(null)}
          onSubmit={handleSubmit}
        />
      )}
    </div>
  );
}
