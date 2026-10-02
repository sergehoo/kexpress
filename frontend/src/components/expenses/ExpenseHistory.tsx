"use client";

import { History } from "lucide-react";

import { Spinner } from "@/components/ui";
import { EXPENSE_STATUS_LABEL, useExpenseHistory, type ExpenseStatus, type HistoryRow } from "@/lib/financeF2";
import { formatDate, formatNumber } from "@/lib/utils";

const ACTION_LABEL: Record<string, string> = {
  create: "Création", submit: "Soumission", send_for_validation: "Transmise à la validation",
  validate: "Validation", reject: "Rejet", request_info: "Complément demandé", pay: "Paiement",
  cancel: "Annulation", discard: "Brouillon abandonné", reconcile: "Réconciliation",
};

const statusLabel = (s: string) => EXPENSE_STATUS_LABEL[s as ExpenseStatus] ?? s;

/** Dernière demande de complément encore en attente (dépense revenue « soumise ») : l'auteur
 *  doit savoir ce que la Finance attend avant de retransmettre. */
export function pendingInfoRequest(rows: HistoryRow[] | undefined, status: ExpenseStatus): HistoryRow | null {
  if (!rows?.length || status !== "submitted") return null;
  const last = rows[rows.length - 1];
  return last.action === "request_info" ? last : null;
}

export function lastRejection(rows: HistoryRow[] | undefined, status: ExpenseStatus): HistoryRow | null {
  if (!rows?.length || status !== "rejected") return null;
  return [...rows].reverse().find((r) => r.action === "reject") ?? null;
}

/** Historique immuable du circuit : qui, quand, ancien → nouveau statut, motif, montant. */
export function ExpenseHistory({ expenseId }: { expenseId: string }) {
  const { data: rows, isLoading, isError } = useExpenseHistory(expenseId);
  return (
    <section className="space-y-2">
      <h4 className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-faint">
        <History className="h-3.5 w-3.5" /> Historique
      </h4>
      {isLoading ? (
        <div className="flex justify-center py-3"><Spinner className="h-4 w-4" /></div>
      ) : isError ? (
        <p className="text-xs text-rose-600">Historique indisponible.</p>
      ) : !rows?.length ? (
        <p className="text-xs text-faint">Aucun événement.</p>
      ) : (
        <ol className="space-y-2 border-l border-line pl-3">
          {rows.map((r, i) => (
            <li key={`${r.at}-${i}`} className="relative text-xs">
              <span className="absolute -left-[17px] top-1 h-2 w-2 rounded-full bg-brand-500" />
              <p className="text-ink">
                <b>{ACTION_LABEL[r.action] ?? r.action}</b>
                {r.from_status && r.to_status && r.from_status !== r.to_status && (
                  <span className="text-muted"> · {statusLabel(r.from_status)} → {statusLabel(r.to_status)}</span>
                )}
              </p>
              <p className="text-[11px] text-faint">
                {r.user} · {formatDate(r.at, true)}
                {r.amount ? ` · ${formatNumber(r.amount)} XOF` : ""}
                {r.cost_center ? ` · ${r.cost_center}` : ""}
              </p>
              {r.comment && <p className="mt-0.5 text-[11px] text-muted">« {r.comment} »</p>}
              {r.reason && <p className="mt-0.5 text-[11px] text-rose-700">Motif : {r.reason}</p>}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
