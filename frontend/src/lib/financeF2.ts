"use client";

/** Finance F2 — couche de données partagée : circuit des dépenses, justificatifs, ajustements,
 *  paramètres, réconciliation, tableau de bord. Montants en texte (Decimal côté API) ;
 *  null = inconnu, jamais 0. La sécurité est côté API : ces hooks ne font qu'afficher. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import type { Money } from "@/lib/queries";

export type ExpenseStatus =
  | "draft" | "submitted" | "to_validate" | "validated" | "paid" | "rejected" | "cancelled";

export const EXPENSE_STATUS_LABEL: Record<ExpenseStatus, string> = {
  draft: "Brouillon", submitted: "Soumise", to_validate: "À valider", validated: "Validée",
  paid: "Payée", rejected: "Rejetée", cancelled: "Annulée",
};

export const EXPENSE_STATUS_TONE: Record<ExpenseStatus, string> = {
  draft: "bg-slate-500/10 text-slate-600", submitted: "bg-sky-500/10 text-sky-700",
  to_validate: "bg-amber-500/10 text-amber-700", validated: "bg-emerald-500/10 text-emerald-700",
  paid: "bg-brand-500/10 text-brand-700", rejected: "bg-rose-500/10 text-rose-700",
  cancelled: "bg-slate-500/10 text-slate-500",
};

export const ATTACHMENT_KINDS = [
  { value: "invoice", label: "Facture" }, { value: "receipt", label: "Reçu" },
  { value: "fuel_ticket", label: "Ticket carburant" }, { value: "toll_ticket", label: "Ticket de péage" },
  { value: "parking", label: "Stationnement" }, { value: "garage_invoice", label: "Facture garage" },
  { value: "charging", label: "Recharge électrique" }, { value: "other", label: "Autre" },
];

export const PAYMENT_METHODS = [
  { value: "transfer", label: "Virement" }, { value: "check", label: "Chèque" },
  { value: "cash", label: "Espèces" }, { value: "mobile_money", label: "Mobile money" },
  { value: "card", label: "Carte" }, { value: "other", label: "Autre" },
];

export const CATEGORY_LABEL: Record<string, string> = {
  toll: "Péage", parking: "Stationnement", washing: "Lavage", road_fees: "Frais de route",
  allowance: "Indemnités", lodging: "Hébergement", repair: "Réparation en mission", fine: "Amende",
  unexpected: "Imprévu", other: "Autre", fuel: "Carburant", maintenance: "Maintenance",
  insurance: "Assurance",
};

export interface Attachment {
  id: string;
  kind: string;
  kind_display: string;
  name: string;
  size?: number;
  uploaded_at?: string;
  /** URL signée, nominative, courte durée : à ouvrir avec `openSecureFile`. null = pas le droit. */
  url: string | null;
}

/** Proposition renvoyée par l'API quand une écriture toucherait un coût figé / un mois clos. */
export interface AdjustmentProposal {
  code: "adjustment_required";
  reason: "trip_frozen" | "mission_frozen" | "period_closed";
  detail: string;
  original_period: string;
  posting_period: string;
  amount: Money;
  subsidiary: string | null;
  source: string;
  source_id: string | null;
  trip: string | null;
  mission: string | null;
  vehicle: string | null;
  category: string;
}

export interface WorkflowExpense {
  id: string;
  label: string;
  amount: string;
  date: string;
  category: string;
  category_display: string;
  status: ExpenseStatus;
  status_display: string;
  vehicle: string | null;
  vehicle_registration: string | null;
  trip: string | null;
  trip_destination: string | null;
  mission: string | null;
  mission_code: string | null;
  cost_center: string | null;
  cost_center_label: string | null;
  supplier: string;
  subsidiary: string;
  subsidiary_name: string;
  author: string | null;
  author_name: string | null;
  receipt: string | null;
  receipt_required: boolean;
  receipt_missing: boolean;
  attachments: Attachment[];
  is_countable: boolean;
  source_type: string;
  source_type_display: string;
  source_id: string | null;
  source_reference: string;
  submitted_at: string | null;
  validated_at: string | null;
  validated_by_name: string | null;
  paid_at: string | null;
  paid_by_name: string | null;
  payment_reference: string;
  payment_method: string;
  accounting_reference: string;
  late: { reason: string; detail: string; original_period: string } | null;
  adjustment: { id: string; status: string; posting_period: string } | null;
  created_at: string;
}

export interface HistoryRow {
  action: string;
  from_status: string;
  to_status: string;
  user: string;
  at: string;
  comment: string;
  reason: string;
  amount: string;
  cost_center: string | null;
  details: Record<string, unknown>;
}

export interface MissionAllocation {
  amount: string;
  allocated: string;
  remaining: string;
  lines: { trip: string; destination: string; subsidiary: string; amount: string; weight: string; rule: string }[];
}

export interface Adjustment {
  id: string;
  original_period_label: string;
  posting_period_label: string;
  source: string;
  source_id: string | null;
  subsidiary: string;
  subsidiary_name: string;
  trip: string | null;
  mission: string | null;
  vehicle: string | null;
  cost_center: string | null;
  expense: string | null;
  category: string;
  amount: string;
  currency: string;
  reason: string;
  status: "pending" | "approved" | "rejected";
  status_display: string;
  created_by: string | null;
  author_name: string | null;
  approved_by: string | null;
  approved_by_name: string | null;
  decided_at: string | null;
  decision_comment: string;
  attachments: Attachment[];
  created_at: string;
}

export interface FinanceSettings {
  receipt_required_from: string | null;
  depreciation_method: string;
  currency: string;
  /** Seuils d'alerte budgétaire par défaut (F3), en % de consommation. */
  budget_alert_thresholds: number[];
  updated_at: string;
}

export interface LegacyExpense {
  id: string;
  original_category: string;
  category_display: string;
  amount: string;
  date: string;
  label: string;
  vehicle: string | null;
  vehicle_registration: string | null;
  subsidiary: string;
  subsidiary_name: string;
  proposed_destination: string;
}

export interface Bucket { key: string; label: string; amount: Money; count: number }

export interface ExpenseDashboard {
  period: string;
  subsidiary: string | null;
  currency: string;
  today: { count: number; amount: Money };
  month: { count: number; amount: Money };
  to_validate: { count: number; amount: Money };
  submitted: { count: number; amount: Money };
  validated: { count: number; amount: Money };
  paid: { count: number; amount: Money };
  rejected: { count: number; amount: Money };
  drafts: { count: number; amount: Money };
  adjustments: { count: number; approved_amount: Money; pending: number };
  without_receipt: { count: number; required_missing: number };
  legacy_to_reconcile: { count: number; amount: Money };
  receipt_threshold: Money;
  by_category: Bucket[];
  by_subsidiary: Bucket[];
  by_vehicle: Bucket[];
  by_cost_center: Bucket[];
  evolution: { period: string; amount: string }[];
  direct_vs_indirect: { direct: string; indirect: string };
}

// --- Lectures ---------------------------------------------------------------------------

interface Paginated<T> { count: number; results: T[] }

export function useWorkflowExpenses(params: Record<string, string>, enabled = true) {
  return useQuery({
    queryKey: ["expenses", params],
    enabled,
    queryFn: async () => (await api.get<Paginated<WorkflowExpense>>("/expenses/", { params })).data,
  });
}

export function useExpenseHistory(id: string | null) {
  return useQuery({
    queryKey: ["expense-history", id],
    enabled: !!id,
    queryFn: async () => (await api.get<HistoryRow[]>(`/expenses/${id}/history/`)).data,
  });
}

export function useExpenseAllocations(id: string | null, enabled = true) {
  return useQuery({
    queryKey: ["expense-allocations", id],
    enabled: enabled && !!id,
    queryFn: async () => (await api.get<MissionAllocation>(`/expenses/${id}/allocations/`)).data,
  });
}

export function useAdjustments(params: Record<string, string> = {}, enabled = true) {
  return useQuery({
    queryKey: ["finance/adjustments", params],
    enabled,
    queryFn: async () => (await api.get<Paginated<Adjustment>>("/finance/adjustments/", { params })).data,
  });
}

export function useFinanceSettings(enabled = true) {
  return useQuery({
    queryKey: ["finance-settings"],
    enabled,
    queryFn: async () => (await api.get<FinanceSettings>("/finance/settings/")).data,
  });
}

export function useLegacyExpenses(enabled = true) {
  return useQuery({
    queryKey: ["finance-reconciliation"],
    enabled,
    queryFn: async () => (await api.get<{ results: LegacyExpense[] }>("/finance/reconciliation/")).data.results,
  });
}

export function useExpenseDashboard(period: string, subsidiary = "", enabled = true) {
  const params: Record<string, string> = { period };
  if (subsidiary) params.subsidiary = subsidiary;
  return useQuery({
    queryKey: ["expense-dashboard", params],
    enabled: enabled && !!period,
    queryFn: async () => (await api.get<ExpenseDashboard>("/finance/expense-dashboard/", { params })).data,
  });
}

export async function costCenterSuggestion(tripId: string): Promise<string | null> {
  const { data } = await api.get<{ cost_center: string | null }>("/expenses/cost-center-suggestion/",
    { params: { trip: tripId } });
  return data.cost_center;
}

// --- Écritures --------------------------------------------------------------------------

const INVALIDATE = ["expenses", "expense-history", "expense-allocations", "expense-dashboard",
  "finance/adjustments", "finance-reconciliation", "subsidiary-costs", "vehicle-cost", "vehicle-costs",
  "trip-cost-sheets", "trip-cost-sheet", "dashboard-stats"];

function useInvalidate() {
  const qc = useQueryClient();
  return () => INVALIDATE.forEach((key) => qc.invalidateQueries({ queryKey: [key] }));
}

export type ExpenseAction =
  | "submit" | "send-for-validation" | "validate" | "reject" | "request-info" | "pay" | "cancel";

/** Transition du circuit. `body` : comment / reason / require_receipt / payment_reference… */
export function useExpenseAction() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ id, action, body = {} }: { id: string; action: ExpenseAction; body?: Record<string, unknown> }) =>
      (await api.post<WorkflowExpense>(`/expenses/${id}/${action}/`, body)).data,
    onSuccess: invalidate,
  });
}

export function useUploadAttachment(target: "expenses" | "finance/adjustments" = "expenses") {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ id, file, kind }: { id: string; file: File; kind: string }) => {
      const form = new FormData();
      form.append("file", file);
      form.append("kind", kind);
      return (await api.post(`/${target}/${id}/attachments/`, form)).data;
    },
    onSuccess: invalidate,
  });
}

export function useDeleteAttachment() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ expenseId, attachmentId }: { expenseId: string; attachmentId: string }) =>
      api.delete(`/expenses/${expenseId}/attachments/${attachmentId}/`),
    onSuccess: invalidate,
  });
}

export interface NewAdjustment {
  original_period: string;
  amount: string;
  reason: string;
  source: string;
  source_id?: string | null;
  subsidiary?: string | null;
  trip?: string | null;
  mission?: string | null;
  vehicle?: string | null;
  cost_center?: string | null;
  category?: string;
}

export function useCreateAdjustment() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async (body: NewAdjustment) => (await api.post<Adjustment>("/finance/adjustments/", body)).data,
    onSuccess: invalidate,
  });
}

export function useDecideAdjustment() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ id, decision, text }: { id: string; decision: "approve" | "reject"; text: string }) =>
      (await api.post<Adjustment>(`/finance/adjustments/${id}/${decision}/`,
        decision === "approve" ? { comment: text } : { reason: text })).data,
    onSuccess: invalidate,
  });
}

export function useSaveFinanceSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: Partial<Pick<FinanceSettings, "receipt_required_from" | "budget_alert_thresholds">>) =>
      (await api.put<FinanceSettings>("/finance/settings/", body)).data,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["finance-settings"] }),
  });
}

export function useReconcile() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ id, destination, params }: { id: string; destination: string; params?: Record<string, unknown> }) =>
      (await api.post(`/finance/reconciliation/${id}/`, { destination, params: params ?? {} })).data,
    onSuccess: invalidate,
  });
}

/** Extrait la proposition d'ajustement d'une erreur API (409 / code adjustment_required). */
export function adjustmentProposal(err: unknown): AdjustmentProposal | null {
  const data = (err as { response?: { data?: Record<string, unknown> } })?.response?.data;
  const proposal = (data?.adjustment_proposal ?? (data?.detail as Record<string, unknown> | undefined)?.adjustment_proposal) as
    AdjustmentProposal | undefined;
  return proposal ?? null;
}
