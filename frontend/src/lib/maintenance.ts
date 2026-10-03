"use client";

/** Plans d'entretien prédictifs — couche de données (gestion : `/maintenance-plans/`).
 *
 *  Une ligne de plan est CALCULÉE par l'API (seuils, distance restante, date prévisionnelle,
 *  niveau) : le front n'invente aucune date. Aucun montant n'y figure. Le bénéficiaire lit
 *  le même format dans « Mon véhicule » (`/carplan/me/tracking/`). */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";

export type PlanLevel = "unknown" | "ok" | "notice" | "alert" | "urgent" | "overdue";
export type Tone = "green" | "blue" | "amber" | "red" | "slate" | "violet" | "cyan";

export const LEVEL_LABEL: Record<PlanLevel, string> = {
  unknown: "À initialiser", ok: "À jour", notice: "Préavis", alert: "Alerte", urgent: "Urgent", overdue: "Dépassé",
};
export const LEVEL_TONE: Record<PlanLevel, Tone> = {
  unknown: "slate", ok: "green", notice: "blue", alert: "amber", urgent: "red", overdue: "red",
};
export const PLAN_KIND_LABEL: Record<string, string> = {
  oil_change: "Vidange moteur", filters: "Filtres", tyres: "Pneumatiques", brakes: "Freinage",
  general_service: "Révision générale", technical_inspection: "Visite technique", insurance: "Assurance",
  other: "Autre opération",
};
export const PLAN_EVENT_LABEL: Record<string, string> = {
  alert: "Alerte", relaunch: "Relance", pace: "Rythme en hausse", cleared: "Alerte levée", done: "Entretien réalisé",
  undone: "Intervention annulée", meter: "Compteur remplacé", config: "Paramètres modifiés",
};

/** Situation d'un plan (ou d'une échéance documentaire : visite technique, assurance). */
export interface PlanRow {
  id: string | null;
  source: "plan" | "insurance" | "technical_inspection";
  vehicle: string;
  registration: string;
  maintenance_type: string | null;
  operation: string;
  kind: string;
  kind_label: string;
  last_done_date: string | null;
  last_done_mileage: number | null;
  interval_km: number | null;
  interval_days: number | null;
  due_mileage: number | null;
  next_date: string | null;
  deadline: string | null;
  date_limit: string | null;
  current_odometer: number;
  remaining_km: number | null;
  km_per_day: number | null;
  forecast_method: "weighted" | "preliminary" | "trips" | null;
  forecast_label: string | null;
  preliminary: boolean;
  forecast_km_date: string | null;
  expected_date: string | null;
  trigger: "km" | "date" | null;
  days_left: number | null;
  level: PlanLevel;
  level_label: string;
  km_reached: boolean;
  date_reached: boolean;
  thresholds: { notice: number; alert: number; urgent: number };
  alert_level: PlanLevel | null;
  alert_notified_at: string | null;
  is_active: boolean;
}

export interface PlanSettings {
  id: string;
  vehicle: string;
  vehicle_registration: string;
  maintenance_type: string;
  type_name: string;
  due_date: string | null;
  due_mileage: number | null;
  is_active: boolean;
  last_done_date: string | null;
  last_done_mileage: number | null;
  interval_km: number | null;
  interval_days: number | null;
}

export interface PlanEvent {
  id: number;
  at: string;
  kind: string;
  kind_label: string;
  level: PlanLevel | null;
  level_label: string | null;
  message: string;
  recipients: number;
  registration: string;
  operation: string;
  plan: string;
  record: string | null;
  actor: string | null;
}

export interface Reliability {
  score: number | null;
  level: "good" | "fair" | "low" | "insufficient";
  label: string;
  readings: number;
  corrections: number;
  anomalies: number;
  factors: string[];
}

export interface MaintenanceOutlook {
  counts: Record<PlanLevel, number>;
  plans: number;
  /** Les 50 échéances les plus pressantes ; `watch_count` les compte toutes. */
  watch: PlanRow[];
  watch_count: number;
  forecasts_count: number;
  forecasts: {
    vehicle: string; registration: string; km_per_day: number | null; method: string | null; label: string | null;
    readings: number | null; reliability: Reliability | null;
  }[];
  events: PlanEvent[];
  interventions: { id: string; registration: string; operation: string; performed_date: string | null; mileage: number | null }[];
}

export interface MaintenanceTypeRef {
  id: string;
  name: string;
  interval_km: number | null;
  interval_days: number | null;
  kind: string;
  kind_display: string;
  combustion_only: boolean;
  notice_days: number;
  alert_days: number;
  urgent_days: number;
}

const KEY = "maintenance-plans";

export interface PlanPage {
  count: number;
  page: number;
  page_size: number;
  results: PlanRow[];
}

/** Plans calculés, paginés par l'API (`page`, `page_size`), filtrables par niveau. */
export function useMaintenancePlans(params: Record<string, string> = {}, enabled = true) {
  return useQuery({
    queryKey: [KEY, "list", params],
    enabled,
    placeholderData: (previous) => previous,
    queryFn: async () => (await api.get<PlanPage>("/maintenance-plans/", { params })).data,
  });
}

export function useMaintenancePlan(id: string | null | undefined) {
  return useQuery({
    queryKey: [KEY, "detail", id],
    enabled: !!id,
    queryFn: async () => (await api.get<PlanRow & { settings: PlanSettings }>(`/maintenance-plans/${id}/`)).data,
  });
}

export function useMaintenanceOutlook(enabled = true) {
  return useQuery({
    queryKey: [KEY, "outlook"],
    enabled,
    queryFn: async () => (await api.get<MaintenanceOutlook>("/maintenance-plans/outlook/")).data,
  });
}

export function usePlanEvents(id: string | null | undefined) {
  return useQuery({
    queryKey: [KEY, "events", id],
    enabled: !!id,
    queryFn: async () => (await api.get<PlanEvent[]>(`/maintenance-plans/${id}/events/`)).data,
  });
}

export function useMaintenanceTypeRefs() {
  return useQuery({
    queryKey: [KEY, "types"],
    queryFn: async () => {
      const { data } = await api.get<MaintenanceTypeRef[] | { results: MaintenanceTypeRef[] }>("/maintenance-types/",
        { params: { page_size: "200" } });
      return Array.isArray(data) ? data : data.results ?? [];
    },
  });
}

function useInvalidatePlans() {
  const qc = useQueryClient();
  return () => {
    void qc.invalidateQueries({ queryKey: [KEY] });
    void qc.invalidateQueries({ queryKey: ["carplan"] });
    void qc.invalidateQueries({ queryKey: ["carplan-me"] });
  };
}

export type PlanBody = Partial<Pick<PlanSettings,
  "due_date" | "due_mileage" | "is_active" | "last_done_date" | "last_done_mileage" | "interval_km" | "interval_days">>;

export function useCreatePlan() {
  const invalidate = useInvalidatePlans();
  return useMutation({
    mutationFn: async (body: PlanBody & { vehicle: string; maintenance_type: string }) =>
      (await api.post<PlanRow & { settings: PlanSettings }>("/maintenance-plans/", body)).data,
    onSuccess: invalidate,
  });
}

export function useUpdatePlan() {
  const invalidate = useInvalidatePlans();
  return useMutation({
    mutationFn: async ({ id, body }: { id: string; body: PlanBody }) =>
      (await api.patch<PlanRow & { settings: PlanSettings }>(`/maintenance-plans/${id}/`, body)).data,
    onSuccess: invalidate,
  });
}

/** « Dans 5 jours », « Aujourd'hui », « Dépassée de 3 jours ». */
/** Libellé d'échéance d'une ligne : seuil kilométrique déjà franchi, sinon jours restants. */
export function dueLabel(r: Pick<PlanRow, "km_reached" | "date_reached" | "days_left">): string {
  return r.km_reached && !r.date_reached ? "Seuil km atteint" : daysLabel(r.days_left);
}

export function daysLabel(days: number | null | undefined): string {
  if (days == null) return "—";
  if (days < 0) return `Dépassée de ${Math.abs(days)} jour${Math.abs(days) > 1 ? "s" : ""}`;
  if (days === 0) return "Aujourd'hui";
  return `Dans ${days} jour${days > 1 ? "s" : ""}`;
}
