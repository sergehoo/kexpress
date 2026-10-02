"use client";

import { Split } from "lucide-react";

import { Spinner } from "@/components/ui";
import { money } from "@/components/finance/RealCostPanel";
import { useExpenseAllocations, type WorkflowExpense } from "@/lib/financeF2";
import { cn, formatNumber } from "@/lib/utils";

const RULE_LABEL: Record<string, string> = { mission_passenger_km: "au passager-km" };

/** Répartition d'une dépense de MISSION entre ses courses (posée à la validation, Σ = montant
 *  au centime). Une dépense tardive est portée par un ajustement : pas de lignes ici. */
export function ExpenseAllocations({ expense }: { expense: WorkflowExpense }) {
  const { data, isLoading, isError } = useExpenseAllocations(expense.id);
  const remaining = data ? Number(data.remaining) : 0;

  return (
    <section className="space-y-2">
      <h4 className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-faint">
        <Split className="h-3.5 w-3.5" /> Répartition sur la mission
      </h4>
      {isLoading ? (
        <div className="flex justify-center py-3"><Spinner className="h-4 w-4" /></div>
      ) : isError || !data ? (
        <p className="text-xs text-rose-600">Répartition indisponible.</p>
      ) : (
        <>
          <div className="grid grid-cols-3 gap-2 rounded-lg bg-surface2 p-3 text-xs text-muted">
            <span>Montant mission<br /><b className="text-ink">{money(data.amount)}</b></span>
            <span>Montant réparti<br /><b className="text-ink">{money(data.allocated)}</b></span>
            <span>Reste à répartir<br />
              <b className={cn(remaining === 0 ? "text-emerald-700" : "text-amber-700")}>{money(data.remaining)}</b>
            </span>
          </div>
          {data.lines.length === 0 ? (
            <p className="text-[11px] text-faint">
              {expense.adjustment
                ? "Dépense portée par un ajustement financier : elle n'est pas répartie sur les courses figées."
                : "La répartition entre les courses de la mission est calculée à la validation."}
            </p>
          ) : (
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-[10px] uppercase tracking-wide text-faint">
                  <th className="py-1 font-medium">Course</th>
                  <th className="py-1 text-right font-medium">Poids</th>
                  <th className="py-1 text-right font-medium">Montant</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {data.lines.map((line) => (
                  <tr key={line.trip}>
                    <td className="py-1.5 text-ink">{line.destination}
                      <span className="ml-1 text-[10px] text-faint">{RULE_LABEL[line.rule] ?? line.rule}</span></td>
                    <td className="py-1.5 text-right text-muted">{formatNumber(line.weight)}</td>
                    <td className="py-1.5 text-right font-medium text-ink">{money(line.amount)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </section>
  );
}
