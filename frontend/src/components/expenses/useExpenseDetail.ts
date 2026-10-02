"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import type { WorkflowExpense } from "@/lib/financeF2";

/** Champs servis par l'API mais absents du type partagé `WorkflowExpense`. */
export type ExpenseDetail = WorkflowExpense & {
  driver?: string | null;
  driver_name?: string | null;
};

/** Une dépense, relue après chaque geste : la clé commence par « expenses », que toutes les
 *  mutations F2 invalident — le détail reflète donc le statut réel, pas la ligne de liste. */
export function useExpenseDetail(id: string | null, fallback?: WorkflowExpense) {
  return useQuery({
    queryKey: ["expenses", "detail", id],
    enabled: !!id,
    placeholderData: fallback as ExpenseDetail | undefined,
    queryFn: async () => (await api.get<ExpenseDetail>(`/expenses/${id}/`)).data,
  });
}

/** Abandon d'un brouillon (DELETE) : l'API le passe « annulé » et le trace — rien n'est effacé. */
export function useDiscardDraft() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (id: string) => { await api.delete(`/expenses/${id}/`); },
    onSuccess: () => {
      for (const key of ["expenses", "expense-history", "expense-dashboard", "dashboard-stats"]) {
        qc.invalidateQueries({ queryKey: [key] });
      }
    },
  });
}
