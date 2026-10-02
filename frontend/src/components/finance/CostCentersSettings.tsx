"use client";

import { useState } from "react";
import { Plus } from "lucide-react";

import { Button, Card, CardBody, CardHeader, CardTitle, EmptyState, Spinner } from "@/components/ui";
import { EntityForm, type Field } from "@/components/EntityForm";
import { RowActions } from "@/components/RowActions";
import { useCostCenters, useSubsidiaries, type CostCenter } from "@/lib/queries";
import { useCrud } from "@/lib/crud";
import { useAuth } from "@/lib/auth";
import { apiError } from "@/lib/api";
import { canFinance } from "@/lib/rbac";

const KINDS = [
  { value: "subsidiary", label: "Filiale" }, { value: "department", label: "Service" },
  { value: "project", label: "Projet" }, { value: "other", label: "Autre" },
];

/** Centres de coût (imputation analytique des dépenses et des courses). */
export function CostCentersSettings() {
  const { me } = useAuth();
  const canWrite = canFinance(me, "manage_budgets");
  const { data, isLoading } = useCostCenters();
  const { data: subs } = useSubsidiaries();
  const crud = useCrud("finance/cost-centers");
  const [form, setForm] = useState<null | { row?: CostCenter }>(null);
  const [error, setError] = useState("");

  const fields: Field[] = [
    { name: "code", label: "Code", required: true },
    { name: "name", label: "Libellé", required: true },
    { name: "kind", label: "Type", type: "select", required: true, options: KINDS },
    { name: "erp_code", label: "Code ERP" },
    ...(!me?.subsidiary
      ? [{ name: "subsidiary", label: "Filiale", type: "select" as const, required: true,
          options: (subs ?? []).map((s) => ({ value: s.id, label: s.name })) }]
      : []),
    { name: "active", label: "Actif", type: "checkbox" },
  ];

  function submit(values: Record<string, unknown>) {
    setError("");
    const opts = { onSuccess: () => setForm(null), onError: (e: unknown) => setError(apiError(e)) };
    if (form?.row) crud.update.mutate({ id: form.row.id, body: values }, opts);
    else crud.create.mutate(values, opts);
  }

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <CardTitle>Centres de coût</CardTitle>
        {canWrite && <Button variant="secondary" onClick={() => { setError(""); setForm({}); }}>
          <Plus className="h-4 w-4" /> Nouveau</Button>}
      </CardHeader>
      <CardBody className="p-0">
        {isLoading ? <div className="flex justify-center py-8"><Spinner /></div>
          : !data?.length ? <EmptyState title="Aucun centre de coût" hint="Imputez dépenses et courses par service ou projet." />
          : (
            <ul className="divide-y divide-line text-sm">
              {data.map((cc) => (
                <li key={cc.id} className="flex items-center gap-3 px-5 py-2.5">
                  <div className="min-w-0 flex-1">
                    <p className="font-medium text-ink">{cc.code} — {cc.name}{!cc.active && <span className="ml-2 text-xs text-faint">(inactif)</span>}</p>
                    <p className="text-[11px] text-muted">{cc.subsidiary_name}{cc.erp_code ? ` · ERP ${cc.erp_code}` : ""}</p>
                  </div>
                  {canWrite && <RowActions label={cc.code} deleting={crud.remove.isPending}
                    onEdit={() => { setError(""); setForm({ row: cc }); }}
                    onDelete={() => crud.remove.mutate(cc.id, { onError: (e) => window.alert(apiError(e)) })} />}
                </li>
              ))}
            </ul>
          )}
      </CardBody>
      {form && (
        <EntityForm open title={form.row ? "Modifier le centre de coût" : "Nouveau centre de coût"} fields={fields}
          initial={(form.row as unknown as Record<string, unknown>) ?? { kind: "department", active: true }}
          submitting={crud.create.isPending || crud.update.isPending} error={error}
          onClose={() => setForm(null)} onSubmit={submit} />
      )}
    </Card>
  );
}
