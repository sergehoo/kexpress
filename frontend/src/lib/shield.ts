"use client";

/** Synchronisation RH Kaydan Shield : couche de données de l'écran d'administration.
 *  Réservé au super administrateur et à l'administrateur entreprise (l'auditeur lit) — la
 *  sécurité est côté API (`/api/shield/…`), ces hooks ne font qu'afficher et transmettre. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import type { Paginated } from "@/lib/types";

export type SyncMode = "full" | "incremental" | "reconcile";
export type SyncStatus = "running" | "succeeded" | "failed" | "interrupted";

export const SYNC_MODE_LABEL: Record<SyncMode, string> = {
  incremental: "Incrémentale",
  full: "Complète",
  reconcile: "Réconciliation",
};

export const SYNC_STATUS_LABEL: Record<SyncStatus, string> = {
  running: "En cours",
  succeeded: "Réussie",
  failed: "Échouée",
  interrupted: "Interrompue",
};

export const CONFLICT_REASON_LABEL: Record<string, string> = {
  duplicate_email: "Email en double dans Shield",
  account_exists: "Compte K-Express existant non lié",
  email_mismatch: "Email Shield différent du compte lié",
  transfer_unmapped: "Mutation vers une filiale non rattachée",
  ignored: "Ignoré",
};

export const EMPLOYEE_STATUS_LABEL: Record<string, string> = {
  active: "Actif",
  on_leave: "En congé",
  suspended: "Suspendu",
  terminated: "Sorti",
};

export interface ShieldRun {
  id: number;
  mode: SyncMode;
  mode_display: string;
  status: SyncStatus;
  status_display: string;
  phase: string | null;
  counters: Record<string, number | string>;
  watermark_start: string | null;
  watermark_end: string | null;
  triggered_by: string | null;
  triggered_by_name: string | null;
  resumed_count: number;
  started_at: string;
  heartbeat_at: string | null;
  finished_at: string | null;
  duration_seconds: number | null;
  error: string;
}

export interface ShieldStatus {
  enabled: boolean;
  configured: boolean;
  base_url: string;
  eligible_statuses: string[];
  max_staleness_hours: number;
  running: ShieldRun | null;
  last_runs: Record<SyncMode, ShieldRun | null>;
  last_success_at: string | null;
  last_complete_success_at: string | null;
  last_reconcile_success_at: string | null;
  stale: boolean;
  /** Réconciliation manquée : rattrapée au prochain passage des 15 minutes. */
  reconcile_due: boolean;
  /** Départs retenus par le garde-fou (dernière exécution échouée) : à confirmer en forçant. */
  departures_held: number | null;
  counts: {
    companies: number;
    companies_mapped: number;
    companies_unmapped_with_employees: number;
    departments: number;
    departments_mapped: number;
    employees: number;
    employees_by_status: Record<string, number>;
    linked: number;
    absent: number;
    stale_employees: number;
    open_conflicts: number;
    conflicts_by_reason: Record<string, number>;
    pending_departures: number;
  };
}

export interface ShieldCompany {
  id: number;
  shield_id: number;
  code: string;
  name: string;
  is_active: boolean;
  subsidiary: string | null;
  subsidiary_name: string | null;
  subsidiary_code: string | null;
  mapping_confirmed_by_name: string | null;
  mapping_confirmed_at: string | null;
  /** Proposition (code identique) — jamais appliquée sans confirmation explicite. */
  suggested_subsidiary: { id: string; name: string; code: string } | null;
  employees_count: number;
  linked_count: number;
  synced_at: string | null;
}

export interface ShieldDepartment {
  id: number;
  shield_id: number;
  code: string;
  name: string;
  company: number | null;
  company_name: string | null;
  company_subsidiary: string | null;
  department: string | null;
  department_name: string | null;
  mapping_confirmed_by_name: string | null;
  mapping_confirmed_at: string | null;
  suggested_department: { id: string; name: string } | null;
  synced_at: string | null;
}

export interface ShieldEmployee {
  id: number;
  shield_id: number;
  email: string;
  first_name: string;
  last_name: string;
  status: string;
  company: number | null;
  company_name: string | null;
  subsidiary_name: string | null;
  department_name: string | null;
  job_title: string;
  matricule: string;
  synced_at: string | null;
  absent_since: string | null;
  conflict: string;
  conflict_display: string;
  conflict_note: string;
  user: string | null;
  user_email: string | null;
  user_is_active: boolean | null;
  linked_at: string | null;
  /** Email Shield accepté au lien alors que le compte en porte un autre (cycle de vie seulement). */
  link_email_accepted: string;
  transfer_pending_since: string | null;
  eligible: boolean;
}

export interface ShieldCandidate {
  id: string;
  email: string;
  full_name: string;
  role: string;
  role_display: string;
  subsidiary_name: string | null;
  is_active: boolean;
  already_linked: boolean;
  /** Fiche Shield à laquelle ce compte est déjà lié (réembauche : « déplacer le lien »). */
  linked_shield_id: number | null;
}

export interface ShieldConflict extends ShieldEmployee {
  candidates: ShieldCandidate[];
  duplicates: { id: number; shield_id: number; first_name: string; last_name: string; status: string; matricule: string }[];
}

const KEYS = ["shield-status", "shield-runs", "shield-companies", "shield-departments", "shield-employees", "shield-conflicts"];

function useInvalidate() {
  const qc = useQueryClient();
  return () => KEYS.forEach((key) => qc.invalidateQueries({ queryKey: [key] }));
}

export function useShieldStatus(enabled = true) {
  return useQuery({
    queryKey: ["shield-status"],
    enabled,
    queryFn: async () => (await api.get<ShieldStatus>("/shield/status/")).data,
    // Une exécution en cours se suit sans recharger la page.
    refetchInterval: (query) => (query.state.data?.running ? 5_000 : 60_000),
  });
}

export function useShieldRuns(params: Record<string, string> = {}, enabled = true) {
  return useQuery({
    queryKey: ["shield-runs", params],
    enabled,
    queryFn: async () =>
      (await api.get<Paginated<ShieldRun>>("/shield/runs/", { params: { page_size: "20", ...params } })).data,
  });
}

export function useShieldCompanies(params: Record<string, string> = {}, enabled = true) {
  return useQuery({
    queryKey: ["shield-companies", params],
    enabled,
    queryFn: async () =>
      (await api.get<Paginated<ShieldCompany>>("/shield/companies/", { params: { page_size: "200", ...params } })).data,
  });
}

export function useShieldDepartments(params: Record<string, string> = {}, enabled = true) {
  return useQuery({
    queryKey: ["shield-departments", params],
    enabled,
    queryFn: async () =>
      (await api.get<Paginated<ShieldDepartment>>("/shield/departments/", { params: { page_size: "200", ...params } })).data,
  });
}

export function useShieldEmployees(params: Record<string, string> = {}, enabled = true) {
  return useQuery({
    queryKey: ["shield-employees", params],
    enabled,
    queryFn: async () =>
      (await api.get<Paginated<ShieldEmployee>>("/shield/employees/", { params: { page_size: "50", ...params } })).data,
  });
}

export function useShieldConflicts(params: Record<string, string> = {}, enabled = true) {
  return useQuery({
    queryKey: ["shield-conflicts", params],
    enabled,
    queryFn: async () =>
      (await api.get<Paginated<ShieldConflict>>("/shield/conflicts/", { params: { page_size: "50", ...params } })).data,
  });
}

export function useTriggerShieldRun() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async (body: { mode: SyncMode; force?: boolean }) =>
      (await api.post<{ queued: boolean; mode: SyncMode }>("/shield/runs/", body)).data,
    onSuccess: invalidate,
  });
}

export function useMapShieldCompany() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ id, subsidiary }: { id: number; subsidiary: string | null }) =>
      (await api.patch<ShieldCompany & { departments_reset: number; effects: Record<string, number> }>(
        `/shield/companies/${id}/`, { subsidiary, confirm: true })).data,
    onSuccess: invalidate,
  });
}

export function useMapShieldDepartment() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ id, department }: { id: number; department: string | null }) =>
      (await api.patch<ShieldDepartment>(`/shield/departments/${id}/`, { department, confirm: true })).data,
    onSuccess: invalidate,
  });
}

export type ResolveBody =
  | { action: "link" | "relink"; user: string; note?: string; allow_email_mismatch?: boolean }
  | { action: "unlink"; note?: string }
  | { action: "ignore"; note?: string }
  | { action: "reopen" };

export function useResolveShieldConflict() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ id, body }: { id: number; body: ResolveBody }) =>
      (await api.post<ShieldConflict>(`/shield/conflicts/${id}/resolve/`, body)).data,
    onSuccess: invalidate,
  });
}

export interface ExactMatchPreview {
  count: number;
  sample: { id: number; shield_id: number; email: string; full_name: string; user: string;
    role_display: string; subsidiary_name: string | null }[];
  /** Fiches laissées au rapprochement manuel, par motif. */
  skipped: Record<string, number>;
}

/** Aperçu du lien groupé des correspondances exactes (rien n'est modifié). */
export function useExactMatchPreview(enabled = true) {
  return useQuery({
    queryKey: ["shield-conflicts", "link-exact"],
    enabled,
    queryFn: async () => (await api.get<ExactMatchPreview>("/shield/conflicts/link-exact/")).data,
  });
}

/** Lien groupé (confirmation explicite) : chaque lien est journalisé côté serveur. */
export function useBulkLinkExact() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async () =>
      (await api.post<{ linked: number; errors: number; remaining: number; skipped: Record<string, number> }>(
        "/shield/conflicts/link-exact/", { confirm: true })).data,
    onSuccess: invalidate,
  });
}

export interface KexpressDepartment {
  id: string;
  name: string;
  subsidiary: string;
  subsidiary_name: string;
}

/** Services K-Express (cibles d'une correspondance de département). */
export function useKexpressDepartments(enabled = true) {
  return useQuery({
    queryKey: ["shield-kx-departments"],
    enabled,
    queryFn: async () => (await api.get<KexpressDepartment[]>("/shield/kexpress-departments/")).data,
    staleTime: 5 * 60_000,
  });
}
