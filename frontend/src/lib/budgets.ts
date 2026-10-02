"use client";

/** F3 — Budgets : couche de données partagée. Montants en texte (Decimal côté API), null =
 *  inconnu. Prévu / engagé / réalisé / décaissé ne se confondent jamais (cf. `definitions`).
 *  La sécurité est côté API : ces hooks ne font qu'afficher. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import type { Money } from "@/lib/queries";

export const BUDGET_CATEGORIES = [
  { value: "energy", label: "Énergie" }, { value: "maintenance", label: "Maintenance" },
  { value: "insurance", label: "Assurance" }, { value: "fixed_charges", label: "Charges fixes" },
  { value: "toll", label: "Péage" }, { value: "parking", label: "Stationnement" },
  { value: "washing", label: "Lavage" }, { value: "road_fees", label: "Frais de route" },
  { value: "allowance", label: "Indemnités" }, { value: "lodging", label: "Hébergement" },
  { value: "repair", label: "Réparation en mission" }, { value: "fine", label: "Amende" },
  { value: "unexpected", label: "Imprévu" }, { value: "other", label: "Autre" },
];

export const BUDGET_STATUS_LABEL: Record<string, string> = {
  draft: "Brouillon", approved: "Approuvé", archived: "Archivé",
};

export interface BudgetLine {
  id: number;
  budget: string;
  month: number | null;
  subsidiary: string | null;
  cost_center: string | null;
  cost_center_label: string | null;
  category: string;
  category_display: string;
  amount: string;
  label: string;
  alert_thresholds: number[] | null;
}

export interface Budget {
  id: string;
  year: number;
  subsidiary: string | null;
  subsidiary_name: string;
  name: string;
  status: "draft" | "approved" | "archived";
  currency: string;
  alert_thresholds: number[] | null;
  created_by: string | null;
  created_by_name: string | null;
  approved_by: string | null;
  approved_by_name: string | null;
  approved_at: string | null;
  lines: BudgetLine[];
  planned_total: string;
  created_at: string;
}

export interface BudgetRevision {
  previous_amount: Money;
  new_amount: string;
  reason: string;
  kind: "initial" | "draft" | "revision";
  author: string | null;
  at: string;
}

export interface BudgetDashboardLine {
  id: number;
  budget: string;
  budget_name: string;
  budget_status: string;
  month: number | null;
  subsidiary: string | null;
  subsidiary_name: string;
  cost_center: string | null;
  cost_center_label: string | null;
  category: string | null;
  label: string;
  planned: Money;
  engaged: Money;
  realised: Money;
  disbursed: Money;
  available: Money;
  rate: Money;
  alert_level: number | null;
  /** Ligne annuelle ramenée au mois filtré (prévu = 1/12). */
  prorated: boolean;
}

export interface BudgetDashboard {
  year: number;
  month: number | null;
  subsidiary: string | null;
  currency: string;
  budgets: { id: string; name: string; status: string; subsidiary: string | null }[];
  totals: { planned: Money; engaged: Money; realised: Money; disbursed: Money; available: Money; rate: Money };
  lines: BudgetDashboardLine[];
  series: { month: number; planned: Money; realised: Money; engaged: Money }[];
  by_category: { key: string; label: string; planned: Money; realised: Money; engaged: Money }[];
  by_subsidiary: { label: string; planned: Money; realised: Money; engaged: Money }[];
  /** Le prévu additionne des lignes de budgets différents qui se recouvrent. */
  planned_overlap: boolean;
  alerts: { line: number; budget: string; threshold: number; rate: string; triggered_at: string }[];
  definitions: Record<"engaged" | "realised" | "disbursed" | "available", string>;
}

export interface BudgetFilters {
  year: number;
  month?: string;
  subsidiary?: string;
  cost_center?: string;
  category?: string;
  budget?: string;
  status?: string;
}

function clean(filters: BudgetFilters): Record<string, string> {
  const out: Record<string, string> = { year: String(filters.year) };
  for (const key of ["month", "subsidiary", "cost_center", "category", "budget", "status"] as const) {
    if (filters[key]) out[key] = String(filters[key]);
  }
  return out;
}

export function useBudgets(params: Record<string, string> = {}, enabled = true) {
  return useQuery({
    queryKey: ["finance/budgets", params],
    enabled,
    queryFn: async () =>
      (await api.get<{ count: number; results: Budget[] }>("/finance/budgets/", { params: { page_size: "100", ...params } })).data.results,
  });
}

export function useBudgetDashboard(filters: BudgetFilters, enabled = true) {
  const params = clean(filters);
  return useQuery({
    queryKey: ["budget-dashboard", params],
    enabled,
    queryFn: async () => (await api.get<BudgetDashboard>("/finance/budgets/dashboard/", { params })).data,
  });
}

export function useBudgetRevisions(lineId: number | null) {
  return useQuery({
    queryKey: ["budget-revisions", lineId],
    enabled: lineId != null,
    queryFn: async () => (await api.get<BudgetRevision[]>(`/finance/budget-lines/${lineId}/revisions/`)).data,
  });
}

function useInvalidate() {
  const qc = useQueryClient();
  return () => ["finance/budgets", "budget-dashboard", "budget-revisions"].forEach((key) =>
    qc.invalidateQueries({ queryKey: [key] }));
}

export function useCreateBudget() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async (body: { year: number; name: string; subsidiary?: string | null; alert_thresholds?: number[] | null }) =>
      (await api.post<Budget>("/finance/budgets/", body)).data,
    onSuccess: invalidate,
  });
}

export function useUpdateBudget() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ id, body }: { id: string; body: { name?: string; alert_thresholds?: number[] | null } }) =>
      (await api.patch<Budget>(`/finance/budgets/${id}/`, body)).data,
    onSuccess: invalidate,
  });
}

export function useBudgetAction() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ id, action }: { id: string; action: "approve" | "archive" }) =>
      (await api.post<Budget>(`/finance/budgets/${id}/${action}/`, {})).data,
    onSuccess: invalidate,
  });
}

export interface NewBudgetLine {
  amount: string;
  month?: number | null;
  subsidiary?: string | null;
  cost_center?: string | null;
  category?: string;
  label?: string;
  alert_thresholds?: number[] | null;
  reason?: string;
}

export function useAddBudgetLine() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ budgetId, body }: { budgetId: string; body: NewBudgetLine }) =>
      (await api.post<BudgetLine>(`/finance/budgets/${budgetId}/lines/`, body)).data,
    onSuccess: invalidate,
  });
}

export function useReviseBudgetLine() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ lineId, amount, reason }: { lineId: number; amount: string; reason: string }) =>
      (await api.patch<BudgetLine>(`/finance/budget-lines/${lineId}/`, { amount, reason })).data,
    onSuccess: invalidate,
  });
}

export function useRemoveBudgetLine() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async (lineId: number) => api.delete(`/finance/budget-lines/${lineId}/`),
    onSuccess: invalidate,
  });
}

/** Export authentifié (en-tête JWT) puis URL objet : un lien simple partirait sans session. */
export async function downloadBudgetExport(filters: BudgetFilters, fmt: "csv" | "xlsx"): Promise<void> {
  const res = await api.get<Blob>("/finance/budgets/export/", { params: { ...clean(filters), fmt }, responseType: "blob" });
  const url = URL.createObjectURL(res.data);
  const link = document.createElement("a");
  link.href = url;
  link.download = `budget_${filters.year}.${fmt}`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}
