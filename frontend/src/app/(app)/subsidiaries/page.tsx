"use client";

import { useState } from "react";
import { Building2, MapPin, Plus } from "lucide-react";

import { Button, Card, CardBody, EmptyState, Spinner } from "@/components/ui";
import { EntityForm, type Field } from "@/components/EntityForm";
import { RowActions } from "@/components/RowActions";
import { useSubsidiaries } from "@/lib/queries";
import { useCrud } from "@/lib/crud";
import { useAuth } from "@/lib/auth";
import { apiError } from "@/lib/api";
import type { Subsidiary } from "@/lib/types";

const FIELDS: Field[] = [
  { name: "name", label: "Nom", required: true, full: true },
  { name: "code", label: "Code", required: true },
  { name: "city", label: "Ville" },
  { name: "email", label: "Email", type: "email" },
  { name: "phone", label: "Téléphone" },
  { name: "address", label: "Adresse", type: "textarea" },
  { name: "is_active", label: "Active", type: "checkbox" },
];

type ModalState = { mode: "create" } | { mode: "edit"; row: Subsidiary } | null;

export default function SubsidiariesPage() {
  const { me } = useAuth();
  const { data: subsidiaries, isLoading } = useSubsidiaries();
  const crud = useCrud("subsidiaries");
  const [modal, setModal] = useState<ModalState>(null);
  const [error, setError] = useState("");
  const canManage = me?.has_company_scope;

  function handleSubmit(values: Record<string, unknown>) {
    setError("");
    const opts = { onSuccess: () => setModal(null), onError: (e: unknown) => setError(apiError(e)) };
    if (modal?.mode === "edit") crud.update.mutate({ id: modal.row.id, body: values }, opts);
    else crud.create.mutate(values, opts);
  }

  if (isLoading) return <div className="flex justify-center py-20"><Spinner className="h-8 w-8" /></div>;

  return (
    <div className="space-y-5">
      {canManage && (
        <div className="flex">
          <Button className="ml-auto" onClick={() => { setError(""); setModal({ mode: "create" }); }}>
            <Plus className="h-4 w-4" /> Nouvelle filiale
          </Button>
        </div>
      )}

      {!subsidiaries || subsidiaries.length === 0 ? (
        <Card><CardBody><EmptyState title="Aucune filiale" /></CardBody></Card>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {subsidiaries.map((s) => (
            <Card key={s.id} className="animate-fade-up transition-shadow hover:shadow-md">
              <CardBody className="space-y-3">
                <div className="flex items-center gap-3">
                  <span className="flex h-11 w-11 items-center justify-center rounded-xl bg-brand-500/10 text-brand-600"><Building2 className="h-5 w-5" /></span>
                  <div className="min-w-0">
                    <p className="truncate font-semibold text-ink">{s.name}</p>
                    <p className="text-xs text-muted">{s.company_name}</p>
                  </div>
                  {canManage && (
                    <div className="ml-auto">
                      <RowActions
                        label={s.name}
                        deleting={crud.remove.isPending}
                        onEdit={() => { setError(""); setModal({ mode: "edit", row: s }); }}
                        onDelete={() => crud.remove.mutate(s.id)}
                      />
                    </div>
                  )}
                </div>
                <div className="flex items-center gap-4 border-t border-line pt-3 text-xs text-muted">
                  <span className="rounded-md bg-surface2 px-2 py-1 font-mono font-medium text-ink">{s.code}</span>
                  <span className="flex items-center gap-1"><MapPin className="h-3.5 w-3.5" /> {s.city || "—"}</span>
                  <span className={"ml-auto rounded-full px-2 py-0.5 text-[11px] font-medium " + (s.is_active ? "bg-emerald-500/10 text-emerald-600" : "bg-slate-500/10 text-slate-500")}>
                    {s.is_active ? "Active" : "Inactive"}
                  </span>
                </div>
              </CardBody>
            </Card>
          ))}
        </div>
      )}

      {modal && (
        <EntityForm
          open
          title={modal.mode === "edit" ? "Modifier la filiale" : "Nouvelle filiale"}
          fields={FIELDS}
          initial={modal.mode === "edit" ? (modal.row as unknown as Record<string, unknown>) : { is_active: true }}
          submitting={crud.create.isPending || crud.update.isPending}
          error={error}
          onClose={() => setModal(null)}
          onSubmit={handleSubmit}
        />
      )}
    </div>
  );
}
