"use client";

import { useState } from "react";

import { Card, CardBody, EmptyState, Select, Spinner } from "@/components/ui";
import { useAudit } from "@/lib/queries";
import { useAuth } from "@/lib/auth";
import { formatDate } from "@/lib/utils";

const ACTIONS = [
  { value: "", label: "Toutes les actions" },
  { value: "create", label: "Création" },
  { value: "update", label: "Modification" },
  { value: "delete", label: "Suppression" },
  { value: "login", label: "Connexion" },
  { value: "export", label: "Export" },
  { value: "access", label: "Accès" },
];

const TONE: Record<string, string> = {
  create: "bg-emerald-500/10 text-emerald-600",
  update: "bg-sky-500/10 text-sky-600",
  delete: "bg-rose-500/10 text-rose-600",
  login: "bg-violet-500/10 text-violet-600",
  export: "bg-amber-500/10 text-amber-600",
  access: "bg-slate-500/10 text-slate-500",
};

export default function AuditPage() {
  const { me } = useAuth();
  const [action, setAction] = useState("");
  const params: Record<string, string> = {};
  if (action) params.action = action;
  const { data, isLoading, isError } = useAudit(params);
  const entries = data?.results ?? [];

  if (!me?.has_company_scope) {
    return (
      <Card>
        <CardBody>
          <EmptyState title="Accès restreint" hint="Le journal d'audit est réservé au périmètre entreprise et aux auditeurs." />
        </CardBody>
      </Card>
    );
  }

  return (
    <div className="space-y-5">
      <Select value={action} onChange={(e) => setAction(e.target.value)} className="sm:w-56">
        {ACTIONS.map((a) => (
          <option key={a.value} value={a.value}>{a.label}</option>
        ))}
      </Select>

      <Card>
        <CardBody className="p-0">
          {isLoading ? (
            <div className="flex justify-center py-16"><Spinner className="h-7 w-7" /></div>
          ) : isError ? (
            <EmptyState title="Accès restreint" />
          ) : entries.length === 0 ? (
            <EmptyState title="Aucune entrée d'audit" />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-faint">
                    <th className="px-5 py-3 font-medium">Date</th>
                    <th className="px-5 py-3 font-medium">Acteur</th>
                    <th className="px-5 py-3 font-medium">Action</th>
                    <th className="px-5 py-3 font-medium">Cible</th>
                    <th className="px-5 py-3 font-medium">IP</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {entries.map((e) => (
                    <tr key={e.id} className="hover:bg-surface2">
                      <td className="px-5 py-3 text-muted">{formatDate(e.created_at, true)}</td>
                      <td className="px-5 py-3 text-ink">{e.actor_name || e.actor_email || "Système"}</td>
                      <td className="px-5 py-3">
                        <span className={"rounded-full px-2.5 py-0.5 text-xs font-medium " + (TONE[e.action] ?? "bg-surface2 text-muted")}>
                          {e.action_display}
                        </span>
                      </td>
                      <td className="px-5 py-3 text-muted">{e.target_repr || "—"}</td>
                      <td className="px-5 py-3 text-faint">{e.ip_address || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardBody>
      </Card>
    </div>
  );
}
