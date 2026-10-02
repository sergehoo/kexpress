"use client";

import { AlertTriangle, Clock } from "lucide-react";

import {
  EXPENSE_STATUS_LABEL, EXPENSE_STATUS_TONE, type ExpenseStatus, type WorkflowExpense,
} from "@/lib/financeF2";
import { cn } from "@/lib/utils";

/** Statuts dans lesquels l'auteur peut encore corriger sa dépense (miroir de
 *  `EDITABLE_STATUSES` côté API ; « à valider » : centre de coût seulement). */
export const EDITABLE: ExpenseStatus[] = ["draft", "submitted", "to_validate"];

/** Sources qui PORTENT le coût : la dépense n'en est qu'une pièce (D2). */
const SOURCE_PIECES = ["fuel_log", "electric_charge", "maintenance", "insurance", "inspection",
  "revision", "vehicle_charge"];

export function ExpenseStatusBadge({ status, className }: { status: ExpenseStatus; className?: string }) {
  return (
    <span className={cn("whitespace-nowrap rounded-full px-2.5 py-0.5 text-[11px] font-medium",
      EXPENSE_STATUS_TONE[status] ?? "bg-surface2 text-muted", className)}>
      {EXPENSE_STATUS_LABEL[status] ?? status}
    </span>
  );
}

/** Pourquoi une ligne n'entre pas dans les totaux. Le statut (brouillon, à valider…) se lit
 *  déjà sur son badge : on ne signale ici que ce qu'il ne dit pas — pièce d'une source,
 *  reprise de l'historique, dépense portée par un ajustement. */
export function NonCountableBadge({ expense }: { expense: WorkflowExpense }) {
  if (expense.is_countable) return null;
  let label: string | null = null;
  let title = "Compté par sa source — pas une seconde fois";
  if (expense.source_type === "legacy") label = "à reprendre";
  else if (SOURCE_PIECES.includes(expense.source_type)) label = "pièce";
  else if (expense.adjustment) {
    label = "via ajustement";
    title = "Comptée par l'ajustement financier qui la porte — pas une seconde fois";
  }
  if (!label) return null;
  return (
    <span className="ml-1 whitespace-nowrap rounded-full bg-amber-500/10 px-2 py-0.5 text-[10px] text-amber-700"
          title={title}>
      {label}
    </span>
  );
}

export function ReceiptMissingBadge({ expense }: { expense: WorkflowExpense }) {
  if (!expense.receipt_missing) return null;
  return (
    <span className="ml-1 whitespace-nowrap rounded-full bg-rose-500/10 px-2 py-0.5 text-[10px] font-medium text-rose-700"
          title="Justificatif obligatoire avant la validation">
      Justificatif requis
    </span>
  );
}

/** Dépense tardive : course clôturée, mission figée ou mois clos. L'utilisateur doit savoir
 *  AVANT la validation que le montant n'ira pas réécrire la période d'origine. */
export function LateExpenseBanner({ late }: { late: WorkflowExpense["late"] }) {
  if (!late) return null;
  return (
    <div className="flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-800 dark:text-amber-300">
      <Clock className="mt-0.5 h-4 w-4 shrink-0" />
      <p>
        <b>Dépense tardive ({late.detail.replace(/\.\s*$/, "")})</b> : elle sera comptabilisée par un ajustement
        financier sur la période ouverte, rattaché à <b>{late.original_period}</b>.
      </p>
    </div>
  );
}

export function Notice({ tone = "info", children }: { tone?: "info" | "warning" | "danger" | "success"; children: React.ReactNode }) {
  const tones = {
    info: "border-sky-500/30 bg-sky-500/5 text-sky-800 dark:text-sky-300",
    warning: "border-amber-500/30 bg-amber-500/5 text-amber-800 dark:text-amber-300",
    danger: "border-rose-500/30 bg-rose-500/5 text-rose-700 dark:text-rose-300",
    success: "border-emerald-500/30 bg-emerald-500/5 text-emerald-800 dark:text-emerald-300",
  };
  return (
    <div className={cn("flex items-start gap-2 rounded-lg border px-3 py-2 text-xs", tones[tone])}>
      {tone !== "success" && tone !== "info" && <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />}
      <div className="min-w-0">{children}</div>
    </div>
  );
}
