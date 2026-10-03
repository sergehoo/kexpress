"use client";

/** Car Plan — véhicules de fonction et de service attribués : couche de données partagée.
 *
 *  Deux familles d'appels :
 *  - GESTION (`/carplan/...`) : profils `carplan.*` de leur périmètre ;
 *  - SELF-SERVICE (`/carplan/me/...`) : le bénéficiaire et SA seule attribution valide (404
 *    sans elle) — aucune donnée financière n'y transite.
 *
 *  Montants en texte (Decimal côté API), `null` = inconnu / non valorisé. La sécurité est côté
 *  API : ces hooks ne font qu'afficher ce qu'elle sert. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { AxiosError } from "axios";

import { api, apiError } from "@/lib/api";
import type { PlanRow, Reliability } from "@/lib/maintenance";
import type { Paginated } from "@/lib/types";

// --- Permissions ------------------------------------------------------------------------

export type CarPlanPerm =
  | "view_carplan"
  | "manage_carplan_assignments"
  | "approve_carplan_assignments"
  | "manage_carplan_policies"
  | "manage_carplan_vehicle_modes"
  | "view_carplan_costs"
  | "manage_carplan_contributions"
  | "export_carplan"
  | "view_carplan_gps";

/** Permission Car Plan effective (affichage seulement : l'API refuse de toute façon). */
export function canCarPlan(me: { carplan_permissions?: string[] } | null | undefined, codename: CarPlanPerm): boolean {
  return !!me?.carplan_permissions?.includes(codename);
}

// --- Libellés -----------------------------------------------------------------------------

export type AssignmentStatus =
  | "requested" | "validated" | "allocated" | "active" | "suspended"
  | "returning" | "returned" | "closed" | "rejected" | "cancelled";

export type AssignmentType = "company_car" | "service";
export type VehicleMode = "pool" | "company_car" | "service";
export type Condition = "good" | "fair" | "poor";
export type Coverage = "company" | "employee" | "shared" | "excluded";
export type MileageDeclaration = "none" | "total" | "split";

export const ASSIGNMENT_STATUS_LABEL: Record<AssignmentStatus, string> = {
  requested: "Demandée",
  validated: "Validée",
  allocated: "Véhicule attribué — remise à faire",
  active: "Active",
  suspended: "Suspendue",
  returning: "Restitution demandée",
  returned: "Restituée",
  closed: "Clôturée",
  rejected: "Refusée",
  cancelled: "Annulée",
};

export type Tone = "green" | "blue" | "amber" | "red" | "slate" | "violet" | "cyan";

export const ASSIGNMENT_STATUS_TONE: Record<string, Tone> = {
  requested: "amber", validated: "blue", allocated: "violet", active: "green", suspended: "red",
  returning: "cyan", returned: "blue", closed: "slate", rejected: "red", cancelled: "slate",
};

export const ASSIGNMENT_TYPE_LABEL: Record<AssignmentType, string> = {
  company_car: "Véhicule de fonction",
  service: "Véhicule de service attribué",
};

export const MODE_LABEL: Record<VehicleMode, string> = {
  pool: "Flotte mutualisée",
  company_car: "Véhicule de fonction",
  service: "Véhicule de service attribué",
};

export const MODE_TONE: Record<string, Tone> = { pool: "slate", company_car: "violet", service: "blue" };

/** Mode d'exploitation qu'exige un type d'attribution (miroir de `MODE_FOR_TYPE`). */
export const MODE_FOR_TYPE: Record<AssignmentType, VehicleMode> = { company_car: "company_car", service: "service" };

export const CONDITION_LABEL: Record<Condition, string> = { good: "Bon", fair: "Correct", poor: "Dégradé" };

export const COVERAGE_LABEL: Record<Coverage, string> = {
  company: "Pris en charge par l'entreprise",
  employee: "À la charge de l'employé",
  shared: "Partagé",
  excluded: "Non couvert",
};

export const MILEAGE_DECLARATION_LABEL: Record<MileageDeclaration, string> = {
  none: "Aucune déclaration",
  total: "Kilométrage total",
  split: "Kilométrage professionnel / privé",
};

export const REQUEST_KIND_LABEL: Record<string, string> = {
  maintenance: "Entretien", replacement: "Remplacement", return: "Restitution", renewal: "Renouvellement",
};

export const REQUEST_STATUS_LABEL: Record<string, string> = {
  open: "Ouverte", accepted: "Acceptée", refused: "Refusée", done: "Traitée",
};
export const REQUEST_STATUS_TONE: Record<string, Tone> = { open: "amber", accepted: "blue", refused: "red", done: "green" };

export const INCIDENT_KIND_LABEL: Record<string, string> = { breakdown: "Panne", incident: "Incident", accident: "Accident" };
export const INCIDENT_STATUS_LABEL: Record<string, string> = { open: "Ouvert", handled: "Pris en charge", closed: "Clos" };
export const INCIDENT_STATUS_TONE: Record<string, Tone> = { open: "red", handled: "amber", closed: "slate" };

export const MODE_CHANGE_STATUS_LABEL: Record<string, string> = { requested: "Demandé", applied: "Appliqué", rejected: "Refusé" };
export const MODE_CHANGE_STATUS_TONE: Record<string, Tone> = { requested: "amber", applied: "green", rejected: "red" };

export const VERSION_STATUS_LABEL: Record<string, string> = { draft: "Brouillon", published: "Publiée", retired: "Retirée" };
export const VERSION_STATUS_TONE: Record<string, Tone> = { draft: "amber", published: "green", retired: "slate" };

export const REPLACEMENT_STATUS_TONE: Record<string, Tone> = { planned: "violet", active: "blue", ended: "slate", cancelled: "slate" };

export const VEHICLE_TYPES = [
  { value: "sedan", label: "Berline" }, { value: "suv", label: "SUV / 4x4" }, { value: "pickup", label: "Pick-up" },
  { value: "van", label: "Utilitaire / Van" }, { value: "bus", label: "Bus / Minibus" }, { value: "truck", label: "Camion" },
  { value: "motorcycle", label: "Moto" }, { value: "other", label: "Autre" },
];

export const FUEL_LABEL: Record<string, string> = {
  gasoline: "Essence", diesel: "Diesel", hybrid: "Hybride", electric: "Électrique", lpg: "GPL", other: "Autre",
};

/** Positions des pneumatiques proposées dans l'état des lieux (clés libres côté API). */
export const TYRE_POSITIONS = [
  { key: "avant_gauche", label: "Avant gauche" },
  { key: "avant_droit", label: "Avant droit" },
  { key: "arriere_gauche", label: "Arrière gauche" },
  { key: "arriere_droit", label: "Arrière droit" },
  { key: "secours", label: "Roue de secours" },
];
export const TYRE_STATES = ["bon", "usé", "à remplacer", "absent"];

export const DEFAULT_EQUIPMENT = [
  "Gilet de sécurité", "Triangle de signalisation", "Extincteur", "Cric", "Clé de roue", "Trousse de secours",
  "Double des clés",
];
export const DEFAULT_DOCUMENTS = ["Carte grise", "Attestation d'assurance", "Visite technique", "Carte carburant"];

export const ANOMALY_SEVERITY_LABEL: Record<string, string> = { minor: "Mineure", major: "Majeure", critical: "Critique" };

/** Libellés de l'historique (`CarPlanEvent.kind`). */
export const EVENT_LABEL: Record<string, string> = {
  requested: "Demande d'attribution",
  validated: "Demande validée",
  rejected: "Demande refusée",
  allocated: "Véhicule attribué",
  handed_over: "Véhicule remis (état des lieux validé)",
  suspended: "Attribution suspendue",
  resumed: "Attribution reprise",
  extended: "Attribution prolongée",
  renewal_requested: "Renouvellement demandé",
  renewed: "Clôturée par renouvellement",
  vehicle_changed: "Changement de véhicule",
  return_requested: "Restitution demandée",
  returned: "Véhicule restitué",
  closed: "Attribution clôturée",
  cancelled: "Attribution annulée",
  released_to_pool: "Mise à disposition au dispatching",
  inspection_handover: "État des lieux de remise réalisé",
  inspection_return: "État des lieux de restitution réalisé",
  inspection_signed: "État des lieux validé",
  mileage_declared: "Relevé kilométrique",
  mileage_corrected: "Relevé kilométrique corrigé",
  meter_replaced: "Compteur remplacé",
  reading_frequency: "Fréquence des relevés modifiée",
  request_maintenance: "Demande d'entretien",
  request_replacement: "Demande de remplacement",
  request_return: "Demande de restitution",
  request_renewal: "Demande de renouvellement",
  request_accepted: "Demande acceptée",
  request_refused: "Demande refusée",
  request_done: "Demande traitée",
  incident_breakdown: "Panne déclarée",
  incident_incident: "Incident déclaré",
  incident_accident: "Accident déclaré",
  incident_handled: "Incident pris en charge",
  incident_closed: "Incident clos",
  replacement_started: "Véhicule de remplacement",
  replacement_ended: "Fin du remplacement",
  alert: "Alerte automatique",
  employee_departure: "Signalement RH : départ",
  employee_transfer: "Signalement RH : changement de filiale",
};

// --- Types ----------------------------------------------------------------------------------

export interface Assignment {
  id: string;
  reference: string;
  beneficiary: string;
  beneficiary_name: string;
  beneficiary_email: string;
  subsidiary: string;
  subsidiary_name: string;
  department: string | null;
  department_name: string | null;
  cost_center: string | null;
  cost_center_label: string | null;
  vehicle: string | null;
  vehicle_registration: string | null;
  vehicle_label: string | null;
  policy_version: string;
  policy_name: string;
  policy_version_number: number;
  assignment_type: AssignmentType;
  assignment_type_display: string;
  start_date: string;
  planned_end_date: string | null;
  actual_return_date: string | null;
  start_mileage: number | null;
  end_mileage: number | null;
  monthly_km_quota: number | null;
  annual_km_quota: number | null;
  monthly_fuel_liters_quota: string | null;
  monthly_energy_kwh_quota: string | null;
  /** Vide : fréquence de la politique. */
  reading_frequency_days: number | null;
  special_conditions: string;
  status: AssignmentStatus;
  status_display: string;
  requested_by_name: string;
  approved_by_name: string | null;
  approved_at: string | null;
  renewal_of: string | null;
  attention: string;
  created_at: string;
  updated_at?: string;
}

export interface AssignmentEvent {
  id: number;
  kind: string;
  from_status: string;
  to_status: string;
  actor_name: string;
  note: string;
  details: Record<string, unknown>;
  at: string;
}

export interface InspectionPhoto {
  id: number;
  image: string | null;
  zone: string;
  caption: string;
  uploaded_at: string;
}

export interface Inspection {
  id: string;
  assignment: string;
  vehicle: string;
  vehicle_registration: string;
  kind: "handover" | "return";
  kind_display: string;
  performed_at: string;
  mileage: number;
  energy_level_pct: number;
  exterior_condition: Condition;
  exterior_notes: string;
  interior_condition: Condition;
  interior_notes: string;
  tyres: Record<string, string>;
  equipment: { item: string; present?: boolean; note?: string }[];
  documents: { item: string; handed?: boolean; note?: string }[];
  anomalies: { zone?: string; description?: string; severity?: string }[];
  observations: string;
  performed_by_name: string | null;
  employee_signed_at: string | null;
  manager_signed_at: string | null;
  manager_signed_by_name: string | null;
  is_signed: boolean;
  pv_pdf: string | null;
  photos: InspectionPhoto[];
}

export interface NewInspection {
  kind: "handover" | "return";
  mileage: number;
  energy_level_pct: number;
  exterior_condition: Condition;
  interior_condition: Condition;
  exterior_notes?: string;
  interior_notes?: string;
  tyres: Record<string, string>;
  equipment: { item: string; present: boolean }[];
  documents: { item: string; handed: boolean }[];
  anomalies: { zone: string; description: string; severity?: string }[];
  observations?: string;
}

export interface Comparison {
  available: boolean;
  km_driven?: number;
  energy_delta_pct?: number;
  gaps?: { kind: string; label: string }[];
  has_gaps?: boolean;
}

export interface Gauge {
  used: number | string;
  quota: number | string | null;
  pct: number | null;
  exceeded: boolean;
}

export interface Usage {
  month: string;
  km_month: Gauge;
  km_year: Gauge;
  fuel_liters_month: Gauge;
  energy_kwh_month: Gauge;
  professional_km_month: number | null;
  private_km_month: number | null;
  declaration: MileageDeclaration;
  last_reading: { date: string; odometer: number; source: string } | null;
}

export interface MileageReading {
  id: number;
  vehicle: string;
  vehicle_registration: string;
  reading_date: string;
  /** Instant réel du relevé (base des moyennes km / jour). */
  recorded_at: string;
  odometer: number;
  professional_km: number | null;
  private_km: number | null;
  source: string;
  source_display: string;
  /** Relevé corrigé par celui-ci (trace : l'original reste listé, `superseded`). */
  corrects: number | null;
  reason: string;
  superseded: boolean;
  corrected_by_id: number | null;
  /** Remplacement de compteur : dernier relevé de l'ancien compteur. */
  previous_odometer: number | null;
  anomaly: string;
  by_manager: boolean;
  created_at: string;
}

export const READING_FREQUENCIES = [{ value: 5, label: "Tous les 5 jours" }, { value: 7, label: "Toutes les semaines" }];

export type ReadingState = "ok" | "due" | "late" | "stale" | "not_required";
export const READING_STATE_TONE: Record<ReadingState, Tone> = {
  ok: "green", due: "amber", late: "red", stale: "red", not_required: "slate",
};

export interface ReadingStatus {
  frequency_days: number;
  frequency_source: "attribution" | "politique";
  required: boolean;
  last_reading: { id: number; date: string; recorded_at: string; odometer: number; source: string; source_display: string } | null;
  next_due: string;
  late_days: number;
  days_since_last: number | null;
  state: ReadingState;
  state_label: string;
  stale: boolean;
}

export interface Pace {
  km_per_day: number | null;
  method: "weighted" | "preliminary" | null;
  label: string;
  readings: number;
  intervals: number;
  span_days: number;
  recent_km_per_day: number | null;
  pace_increase: boolean;
  last_at: string | null;
  last_odometer: number | null;
}

/** Suivi kilométrique et entretien prévisionnel d'une attribution — sans montant. */
export interface Tracking {
  reading: ReadingStatus;
  pace: Pace | null;
  reliability: Reliability;
  current_odometer: number | null;
  maintenance: PlanRow[];
  next_operation: PlanRow | null;
  /** Relevé que le bénéficiaire peut encore corriger lui-même (sa dernière déclaration, < 48 h). */
  correctable_reading: number | null;
  last_reminder?: { step: string; label: string; at: string } | null;
}

export interface FollowupRow {
  assignment: string;
  reference: string;
  beneficiary_name: string;
  subsidiary_name: string;
  vehicle: string;
  registration: string;
  fuel_type: string;
  status: AssignmentStatus;
  reading: ReadingStatus;
  pace: Pace | null;
  reliability: Reliability;
  current_odometer: number | null;
  next_operation: PlanRow | null;
  urgent_operations: number;
  last_reminder: { step: string; label: string; at: string } | null;
}

export interface Replacement {
  id: string;
  assignment: string;
  assignment_reference: string;
  vehicle: string;
  vehicle_registration: string;
  vehicle_label: string;
  start_date: string;
  end_date: string;
  actual_end_date: string | null;
  reason?: string;
  status: "planned" | "active" | "ended" | "cancelled";
  status_display: string;
  created_at: string;
}

export interface CostMonth {
  period: string;
  provisional: boolean;
  vehicles: { vehicle: string; registration: string; kind: string; days: number; total: string | null }[];
  energy: string | null;
  fixed: string | null;
  other: string | null;
  absorbed_by_pool_trips: string | null;
  total: string | null;
  km: number;
  cost_per_km: string | null;
}

export interface AssignmentCosts {
  months: CostMonth[];
  total: string | null;
  km: number;
  cost_per_km: string | null;
  employee_contributions: string | null;
  provisional: boolean;
}

export interface Contribution {
  id: string;
  period: string;
  amount: string;
  note: string;
  recorded_by: string;
  created_at: string;
}

export interface CarPlanRequest {
  id: string;
  assignment: string;
  assignment_reference: string;
  kind: string;
  kind_display: string;
  description: string;
  desired_date: string | null;
  status: "open" | "accepted" | "refused" | "done";
  status_display: string;
  created_by_name: string | null;
  handled_by_name: string | null;
  response: string;
  maintenance: string | null;
  created_at: string;
  updated_at: string;
}

export interface CarPlanIncident {
  id: string;
  assignment: string;
  assignment_reference: string;
  vehicle: string;
  vehicle_registration: string;
  kind: string;
  kind_display: string;
  occurred_at: string;
  location: string;
  description: string;
  vehicle_drivable: boolean;
  photo: string | null;
  status: "open" | "handled" | "closed";
  status_display: string;
  maintenance: string | null;
  created_at: string;
}

export interface VehicleModeRow {
  id: string;
  registration: string;
  label: string;
  vehicle_type: string;
  fuel_type: string;
  subsidiary: string | null;
  subsidiary_name: string | null;
  mode: VehicleMode;
  mode_display: string;
  holder: string | null;
  pending_change: { id: string; to_mode: VehicleMode } | null;
}

export interface ModeChange {
  id: string;
  vehicle: string;
  vehicle_registration: string;
  from_mode: VehicleMode;
  to_mode: VehicleMode;
  reason: string;
  status: "requested" | "applied" | "rejected";
  requested_by_name: string;
  decided_by_name: string | null;
  decided_at: string | null;
  decision_note: string;
  created_at: string;
}

export interface PoolRelease {
  id: string;
  vehicle: string;
  vehicle_registration: string;
  assignment: string | null;
  starts_at: string;
  ends_at: string;
  reason: string;
  revoked_at: string | null;
  created_at: string;
}

export interface AllowedVehicleRule {
  vehicle_type: string;
  max_purchase_value?: string | null;
}

export interface PolicyVersion {
  id: string;
  policy: string;
  number: number;
  status: "draft" | "published" | "retired";
  status_display: string;
  effective_from: string;
  eligible_categories: string[];
  allowed_vehicles: AllowedVehicleRule[];
  assignment_types: AssignmentType[];
  max_duration_months: number | null;
  professional_use: string;
  private_use_allowed: boolean;
  private_use: string;
  mileage_declaration: MileageDeclaration;
  reading_frequency_days: number;
  monthly_km_limit: number | null;
  annual_km_limit: number | null;
  monthly_fuel_liters_limit: string | null;
  monthly_energy_kwh_limit: string | null;
  tolls_coverage: Coverage;
  parking_coverage: Coverage;
  maintenance_coverage: Coverage;
  /** Absent sans `view_carplan_costs` (donnée financière). */
  employee_contribution_monthly?: string | null;
  contribution_terms: string;
  return_conditions: string;
  replacement_conditions: string;
  published_at: string | null;
  published_by_name: string | null;
  created_at: string;
}

export interface Policy {
  id: string;
  subsidiary: string | null;
  subsidiary_name: string;
  code: string;
  name: string;
  is_active: boolean;
  versions: PolicyVersion[];
  created_at: string;
}

export interface EmployeeCategory {
  id: string;
  subsidiary: string | null;
  code: string;
  label: string;
  rank: number;
  is_active: boolean;
}

export interface CarPlanOverview {
  by_status: Record<string, number>;
  by_type: Record<string, number>;
  vehicles_by_mode: Record<VehicleMode, number>;
  active: number;
  expiring_30_days: number;
  late_returns: number;
  awaiting_validation: number;
  awaiting_handover: number;
  flagged: number;
  open_requests: number;
  open_incidents: number;
  quota_overruns: number;
  km_this_month: number;
}

export interface CostRow {
  assignment: string;
  reference: string;
  beneficiary: string;
  subsidiary_name: string;
  department_name: string | null;
  cost_center_label: string | null;
  vehicle: string | null;
  assignment_type: AssignmentType;
  status: AssignmentStatus;
  km: number;
  total: string | null;
  energy: string | null;
  fixed: string | null;
  other: string | null;
  cost_per_km: string | null;
  employee_contributions: string | null;
  provisional: boolean;
}

export interface CostGroup {
  key: string;
  total: string | null;
  km: number;
  count: number;
  cost_per_km: string | null;
}

export type CostAxis = "subsidiary_name" | "department_name" | "vehicle" | "cost_center_label";

export interface CarPlanCosts {
  year: number;
  month: number | null;
  rows: CostRow[];
  groups: Record<CostAxis, CostGroup[]>;
  series: { period: string; total: string | null }[];
  total: string | null;
  km: number;
  cost_per_km: string | null;
  employee_contributions: string | null;
}

export interface CarPlanDashboard {
  overview: CarPlanOverview;
  /** `null` sans `view_carplan_costs` : aucun montant n'est servi. */
  costs: CarPlanCosts | null;
}

export interface DashboardFilters {
  year: number;
  month?: string;
  subsidiary?: string;
  department?: string;
  cost_center?: string;
  assignment_type?: string;
  status?: string;
  vehicle?: string;
}

// --- Self-service ---------------------------------------------------------------------------

export interface MyVehicleInfo {
  id: string;
  registration: string;
  brand: string;
  model: string;
  vehicle_type: string;
  fuel_type: string;
  mileage: number;
  tank_capacity_liters: string | null;
  battery_capacity_kwh: string | null;
}

export interface MyConditions {
  policy: string;
  version: number;
  professional_use: string;
  private_use_allowed: boolean;
  private_use: string;
  mileage_declaration: MileageDeclaration;
  reading_frequency_days?: number;
  tolls: string;
  parking: string;
  maintenance: string;
  return_conditions: string;
  replacement_conditions: string;
}

export interface MyCompliance {
  compliant: boolean;
  issues: { code: string; label: string }[];
  insurance_expiry: string | null;
  insurance_days_left: number | null;
  inspection_next_date: string | null;
  inspection_days_left: number | null;
  revision_interval_km?: number;
  next_revision_km: number | null;
  revision_remaining_km: number | null;
}

export interface MyAssignment {
  id: string;
  reference: string;
  assignment_type: AssignmentType;
  assignment_type_display: string;
  status: AssignmentStatus;
  status_display: string;
  start_date: string;
  planned_end_date: string | null;
  start_mileage: number | null;
  monthly_km_quota: number | null;
  annual_km_quota: number | null;
  monthly_fuel_liters_quota: string | null;
  monthly_energy_kwh_quota: string | null;
  reading_frequency_days?: number | null;
  special_conditions: string;
  vehicle: MyVehicleInfo | null;
  conditions: MyConditions;
  attention: string;
}

export interface MyVehicle extends MyAssignment {
  usage: Usage;
  compliance: MyCompliance | null;
  pending_inspection: Inspection | null;
  replacement: {
    vehicle_registration: string;
    vehicle_label: string;
    start_date: string;
    end_date: string;
    status: string;
    status_display: string;
  } | null;
}

// --- Outils ---------------------------------------------------------------------------------

/** Liste servie brute ou paginée (DRF) : on la ramène à un tableau. */
export function rows<T>(data: T[] | Paginated<T> | undefined | null): T[] {
  if (!data) return [];
  return Array.isArray(data) ? data : data.results ?? [];
}

export function httpStatus(err: unknown): number | undefined {
  return (err as AxiosError)?.response?.status;
}

/** Message d'erreur affichable : refus métier (400) tel quel ; 403 / 404 explicités. */
export function carPlanError(err: unknown, fallback = "Une erreur est survenue."): string {
  const status = httpStatus(err);
  if (status === 404) return "Introuvable, ou hors de votre périmètre.";
  if (status === 403) return apiError(err, "Action non autorisée pour votre profil.");
  return apiError(err, fallback);
}

/** Pas de nouvel essai sur un refus (4xx) : 404 = « pas d'accès », c'est une réponse. */
function retry(count: number, err: unknown) {
  const status = httpStatus(err);
  return (status === undefined || status >= 500) && count < 2;
}

const KEY = "carplan";
const ME = "carplan-me";

function useInvalidate() {
  const qc = useQueryClient();
  return () => {
    void qc.invalidateQueries({ queryKey: [KEY] });
    void qc.invalidateQueries({ queryKey: [ME] });
    // Un relevé recalcule les prévisions d'entretien.
    void qc.invalidateQueries({ queryKey: ["maintenance-plans"] });
  };
}

function cleanParams(params: Record<string, string | number | undefined | null>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") out[k] = String(v);
  }
  return out;
}

// --- Tableau de bord & export -----------------------------------------------------------------

export function useCarPlanDashboard(filters: DashboardFilters, enabled = true) {
  const params = cleanParams({ ...filters });
  return useQuery({
    queryKey: [KEY, "dashboard", params],
    enabled,
    retry,
    queryFn: async () => (await api.get<CarPlanDashboard>("/carplan/dashboard/", { params })).data,
  });
}

/** Export authentifié (en-tête JWT) puis URL objet : un lien simple partirait sans session. */
export async function downloadCarPlanExport(filters: DashboardFilters, fmt: "csv" | "xlsx"): Promise<void> {
  const params = { ...cleanParams({ ...filters }), export_format: fmt };
  try {
    const res = await api.get<Blob>("/carplan/dashboard/export/", { params, responseType: "blob" });
    const url = URL.createObjectURL(res.data);
    const link = document.createElement("a");
    link.href = url;
    const period = filters.month ? `${filters.year}-${String(filters.month).padStart(2, "0")}` : String(filters.year);
    link.download = `car-plan-${period}.${fmt}`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10_000);
  } catch (err) {
    // En `blob`, le corps d'erreur DRF arrive lui aussi en Blob : on le relit.
    const data = (err as { response?: { data?: unknown } })?.response?.data;
    if (data instanceof Blob) {
      try {
        const parsed = JSON.parse(await data.text()) as Record<string, unknown>;
        const detail = typeof parsed.detail === "string" ? parsed.detail : Object.values(parsed).map(String)[0];
        if (detail) throw new Error(detail);
      } catch (inner) {
        if (inner instanceof Error && !(inner instanceof SyntaxError)) throw inner;
      }
    }
    throw new Error(carPlanError(err, "Export impossible."));
  }
}

// --- Attributions ----------------------------------------------------------------------------

export function useAssignments(params: Record<string, string>, enabled = true) {
  return useQuery({
    queryKey: [KEY, "assignments", params],
    enabled,
    retry,
    queryFn: async () => {
      const { data } = await api.get<Paginated<Assignment> | Assignment[]>("/carplan/assignments/", { params });
      return Array.isArray(data) ? { count: data.length, next: null, previous: null, results: data } : data;
    },
  });
}

export function useAssignment(id: string | null | undefined) {
  return useQuery({
    queryKey: [KEY, "assignment", id],
    enabled: !!id,
    retry,
    queryFn: async () => (await api.get<Assignment>(`/carplan/assignments/${id}/`)).data,
  });
}

function useAssignmentSub<T>(id: string | null | undefined, sub: string, enabled = true) {
  return useQuery({
    queryKey: [KEY, "assignment", id, sub],
    enabled: !!id && enabled,
    retry,
    queryFn: async () => (await api.get<T>(`/carplan/assignments/${id}/${sub}/`)).data,
  });
}

export const useAssignmentEvents = (id?: string | null) => useAssignmentSub<AssignmentEvent[]>(id, "events");
export const useAssignmentInspections = (id?: string | null) => useAssignmentSub<Inspection[]>(id, "inspections");
export const useAssignmentComparison = (id?: string | null, enabled = true) =>
  useAssignmentSub<Comparison>(id, "comparison", enabled);
export const useAssignmentUsage = (id?: string | null) => useAssignmentSub<Usage>(id, "usage");
export const useAssignmentMileage = (id?: string | null) => useAssignmentSub<MileageReading[]>(id, "mileage");
export const useAssignmentTracking = (id?: string | null, enabled = true) =>
  useAssignmentSub<Tracking>(id, "tracking", enabled);
export const useAssignmentReplacements = (id?: string | null) => useAssignmentSub<Replacement[]>(id, "replacements");
export const useAssignmentCosts = (id?: string | null, enabled = true) =>
  useAssignmentSub<AssignmentCosts>(id, "costs", enabled);
export const useAssignmentContributions = (id?: string | null, enabled = true) =>
  useAssignmentSub<Contribution[]>(id, "contributions", enabled);

export interface NewAssignment {
  beneficiary: string;
  policy: string;
  assignment_type: AssignmentType;
  start_date: string;
  planned_end_date?: string | null;
  department?: string | null;
  cost_center?: string | null;
  special_conditions?: string;
  monthly_km_quota?: number | null;
  annual_km_quota?: number | null;
}

export function useCreateAssignment() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async (body: NewAssignment) => (await api.post<Assignment>("/carplan/assignments/", body)).data,
    onSuccess: invalidate,
  });
}

export type AssignmentAction =
  | "validate" | "reject" | "allocate" | "suspend" | "resume" | "extend" | "renew"
  | "change-vehicle" | "request-return" | "close" | "cancel" | "release";

/** Geste du circuit. `renew` répond la NOUVELLE attribution, `release` la mise à disposition. */
export function useAssignmentAction() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ id, action, body = {} }: { id: string; action: AssignmentAction; body?: Record<string, unknown> }) =>
      (await api.post<{ id: string; reference?: string; status?: AssignmentStatus }>(
        `/carplan/assignments/${id}/${action}/`, body)).data,
    onSuccess: invalidate,
  });
}

// --- États des lieux -----------------------------------------------------------------------

export function useCreateInspection() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ assignmentId, body }: { assignmentId: string; body: NewInspection }) =>
      (await api.post<Inspection>(`/carplan/assignments/${assignmentId}/inspections/`, body)).data,
    onSuccess: invalidate,
  });
}

/** Validation d'un état des lieux : `manager` (gestion) ou `beneficiary` (self-service). */
export function useSignInspection(as: "manager" | "beneficiary") {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async (inspectionId: string) => {
      const url = as === "manager" ? `/carplan/inspections/${inspectionId}/sign/` : `/carplan/me/inspections/${inspectionId}/sign/`;
      return (await api.post<Inspection>(url, {})).data;
    },
    onSuccess: invalidate,
  });
}

export function useUploadInspectionPhoto(as: "manager" | "beneficiary") {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ inspectionId, file, zone, caption }: { inspectionId: string; file: File; zone?: string; caption?: string }) => {
      const form = new FormData();
      form.append("image", file);
      if (zone) form.append("zone", zone);
      if (caption) form.append("caption", caption);
      const url = as === "manager" ? `/carplan/inspections/${inspectionId}/photos/` : `/carplan/me/inspections/${inspectionId}/photos/`;
      return (await api.post<InspectionPhoto>(url, form)).data;
    },
    onSuccess: invalidate,
  });
}

// --- Relevés, remplacements, participations ----------------------------------------------------

export function useManagerMileage() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ assignmentId, odometer, reading_date }: { assignmentId: string; odometer: number; reading_date?: string }) =>
      (await api.post<MileageReading>(`/carplan/assignments/${assignmentId}/mileage/`, cleanParams({ odometer, reading_date }))).data,
    onSuccess: invalidate,
  });
}

/** Correction tracée d'un relevé par un gestionnaire (motif obligatoire). */
export function useCorrectMileage() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ assignmentId, readingId, odometer, reason }: {
      assignmentId: string; readingId: number; odometer: number; reason: string;
    }) => (await api.post<MileageReading>(`/carplan/assignments/${assignmentId}/mileage/${readingId}/correct/`,
      { odometer, reason })).data,
    onSuccess: invalidate,
  });
}

export function useMeterReplacement() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ assignmentId, body }: {
      assignmentId: string; body: { old_odometer: number; new_odometer: number; reason: string; recorded_at?: string };
    }) => (await api.post<MileageReading>(`/carplan/assignments/${assignmentId}/meter-replacement/`, body)).data,
    onSuccess: invalidate,
  });
}

export function useReadingFrequency() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ assignmentId, days }: { assignmentId: string; days: number | null }) =>
      (await api.post<Assignment>(`/carplan/assignments/${assignmentId}/reading-frequency/`, { days })).data,
    onSuccess: invalidate,
  });
}

export type FollowupFilter = "" | "late" | "maintenance" | "unreliable";

export interface FollowupPage {
  count: number;
  page: number;
  page_size: number;
  results: FollowupRow[];
  /** Compteurs sur TOUTES les attributions en cours du périmètre (indépendants du filtre). */
  counts: { total: number; late: number; maintenance: number; unreliable: number };
}

/** Gestion : relevés en retard, fiabilité et entretien le plus proche des attributions en cours.
 *  Filtre et pagination appliqués par l'API. */
export function useMileageFollowup({ state = "", page = 1, pageSize = 50 }: {
  state?: FollowupFilter; page?: number; pageSize?: number;
} = {}, enabled = true) {
  return useQuery({
    queryKey: [KEY, "mileage-followup", state, page, pageSize],
    enabled,
    retry,
    placeholderData: (previous) => previous,
    queryFn: async () => (await api.get<FollowupPage>("/carplan/mileage-followup/", {
      params: { ...(state ? { state } : {}), page: String(page), page_size: String(pageSize) },
    })).data,
  });
}

export function useStartReplacement() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ assignmentId, body }: {
      assignmentId: string; body: { vehicle: string; start_date: string; end_date: string; reason: string };
    }) => (await api.post<Replacement>(`/carplan/assignments/${assignmentId}/replacements/`, body)).data,
    onSuccess: invalidate,
  });
}

export function useEndReplacement() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ id, on_date }: { id: string; on_date?: string }) =>
      (await api.post<Replacement>(`/carplan/replacements/${id}/end/`, on_date ? { on_date } : {})).data,
    onSuccess: invalidate,
  });
}

export function useRecordContribution() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ assignmentId, body }: { assignmentId: string; body: { period: string; amount: string; note?: string } }) =>
      (await api.post<Contribution>(`/carplan/assignments/${assignmentId}/contributions/`, body)).data,
    onSuccess: invalidate,
  });
}

// --- Demandes & incidents (gestion) -----------------------------------------------------------

export function useCarPlanRequests(params: Record<string, string>, enabled = true) {
  return useQuery({
    queryKey: [KEY, "requests", params],
    enabled,
    retry,
    queryFn: async () =>
      rows((await api.get<Paginated<CarPlanRequest> | CarPlanRequest[]>("/carplan/requests/", { params: { page_size: "100", ...params } })).data),
  });
}

export function useHandleRequest() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ id, body }: {
      id: string; body: { accept: boolean; response?: string; maintenance_type?: string; scheduled_date?: string };
    }) => (await api.post<CarPlanRequest>(`/carplan/requests/${id}/handle/`, body)).data,
    onSuccess: invalidate,
  });
}

export function useCompleteRequest() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ id, response }: { id: string; response?: string }) =>
      (await api.post<CarPlanRequest>(`/carplan/requests/${id}/complete/`, response ? { response } : {})).data,
    onSuccess: invalidate,
  });
}

export function useCarPlanIncidents(params: Record<string, string>, enabled = true) {
  return useQuery({
    queryKey: [KEY, "incidents", params],
    enabled,
    retry,
    queryFn: async () =>
      rows((await api.get<Paginated<CarPlanIncident> | CarPlanIncident[]>("/carplan/incidents/", { params: { page_size: "100", ...params } })).data),
  });
}

export function useIncidentAction() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ id, action, body }: { id: string; action: "handle" | "close"; body: { maintenance_type?: string; note?: string } }) =>
      (await api.post<CarPlanIncident>(`/carplan/incidents/${id}/${action}/`, body)).data,
    onSuccess: invalidate,
  });
}

// --- Véhicules : modes d'exploitation ------------------------------------------------------------

export function useCarPlanVehicles(mode?: VehicleMode | "", enabled = true) {
  const params = cleanParams({ mode });
  return useQuery({
    queryKey: [KEY, "vehicles", params],
    enabled,
    retry,
    queryFn: async () => rows((await api.get<VehicleModeRow[] | Paginated<VehicleModeRow>>("/carplan/vehicles/", { params })).data),
  });
}

export function useRequestModeChange() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ vehicleId, to_mode, reason }: { vehicleId: string; to_mode: VehicleMode; reason: string }) =>
      (await api.post<ModeChange>(`/carplan/vehicles/${vehicleId}/request-mode/`, { to_mode, reason })).data,
    onSuccess: invalidate,
  });
}

export function useModeChanges(params: Record<string, string> = {}, enabled = true) {
  return useQuery({
    queryKey: [KEY, "mode-changes", params],
    enabled,
    retry,
    queryFn: async () =>
      rows((await api.get<Paginated<ModeChange> | ModeChange[]>("/carplan/mode-changes/", { params: { page_size: "100", ...params } })).data),
  });
}

export function useDecideModeChange() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ id, approve, note }: { id: string; approve: boolean; note?: string }) =>
      (await api.post<ModeChange>(`/carplan/mode-changes/${id}/decide/`, { approve, note: note ?? "" })).data,
    onSuccess: invalidate,
  });
}

// --- Mises à disposition au dispatching -------------------------------------------------------

export function useReleases(enabled = true) {
  return useQuery({
    queryKey: [KEY, "releases"],
    enabled,
    retry,
    queryFn: async () =>
      rows((await api.get<Paginated<PoolRelease> | PoolRelease[]>("/carplan/releases/", { params: { page_size: "100" } })).data),
  });
}

export function useRevokeRelease() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async (id: string) => (await api.post<PoolRelease>(`/carplan/releases/${id}/revoke/`, {})).data,
    onSuccess: invalidate,
  });
}

// --- Référentiels : catégories, politiques, profils --------------------------------------------

export function useCategories(enabled = true) {
  return useQuery({
    queryKey: [KEY, "categories"],
    enabled,
    retry,
    queryFn: async () =>
      rows((await api.get<Paginated<EmployeeCategory> | EmployeeCategory[]>("/carplan/categories/", { params: { page_size: "200" } })).data),
  });
}

export function useSaveCategory() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ id, body }: { id?: string; body: Partial<Omit<EmployeeCategory, "id">> }) =>
      id
        ? (await api.patch<EmployeeCategory>(`/carplan/categories/${id}/`, body)).data
        : (await api.post<EmployeeCategory>("/carplan/categories/", body)).data,
    onSuccess: invalidate,
  });
}

export function usePolicies(enabled = true) {
  return useQuery({
    queryKey: [KEY, "policies"],
    enabled,
    retry,
    queryFn: async () =>
      rows((await api.get<Paginated<Policy> | Policy[]>("/carplan/policies/", { params: { page_size: "200" } })).data),
  });
}

export function useCreatePolicy() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async (body: { code: string; name: string; subsidiary?: string | null; effective_from?: string }) =>
      (await api.post<Policy>("/carplan/policies/", body)).data,
    onSuccess: invalidate,
  });
}

export function useNewPolicyVersion() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ policyId, effective_from }: { policyId: string; effective_from: string }) =>
      (await api.post<PolicyVersion>(`/carplan/policies/${policyId}/new-version/`, { effective_from })).data,
    onSuccess: invalidate,
  });
}

/** Champs modifiables d'une version BROUILLON (`services.POLICY_FIELDS` + date + catégories). */
export type PolicyDraftBody = Partial<Pick<PolicyVersion,
  | "effective_from" | "allowed_vehicles" | "assignment_types" | "max_duration_months" | "professional_use"
  | "private_use_allowed" | "private_use" | "mileage_declaration" | "reading_frequency_days"
  | "monthly_km_limit" | "annual_km_limit"
  | "monthly_fuel_liters_limit" | "monthly_energy_kwh_limit" | "tolls_coverage" | "parking_coverage"
  | "maintenance_coverage" | "employee_contribution_monthly" | "contribution_terms" | "return_conditions"
  | "replacement_conditions" | "eligible_categories">>;

export function useUpdatePolicyVersion() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ policyId, versionId, body }: { policyId: string; versionId: string; body: PolicyDraftBody }) =>
      (await api.patch<PolicyVersion>(`/carplan/policies/${policyId}/versions/${versionId}/`, body)).data,
    onSuccess: invalidate,
  });
}

export function usePublishPolicyVersion() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ policyId, versionId }: { policyId: string; versionId: string }) =>
      (await api.post<PolicyVersion>(`/carplan/policies/${policyId}/versions/${versionId}/publish/`, {})).data,
    onSuccess: invalidate,
  });
}

export type CarPlanProfileRow = {
  user: string; user_name: string; category: string | null; category_label: string | null; job_title: string;
};

/** Profil Car Plan actuel d'un employé (`GET /carplan/profiles/?user=`), ou null s'il n'en a pas. */
export async function fetchProfile(userId: string): Promise<CarPlanProfileRow | null> {
  const rows = (await api.get<CarPlanProfileRow[]>("/carplan/profiles/", { params: { user: userId } })).data;
  return rows[0] ?? null;
}

/** Catégorie Car Plan (et fonction) d'un employé — `POST /carplan/profiles/`. */
export function useSaveProfile() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async (body: { user: string; category: string | null; job_title?: string }) =>
      (await api.post<{ user: string; category: string | null; job_title: string }>("/carplan/profiles/", body)).data,
    onSuccess: invalidate,
  });
}

// --- Accès exceptionnels à la position (GPS) -----------------------------------------------------

export type GpsMotive = "theft" | "accident" | "security" | "legal" | "other";
export type GpsGrantStatus = "requested" | "approved" | "rejected" | "revoked";

export const GPS_MOTIVE_LABEL: Record<GpsMotive, string> = {
  theft: "Vol ou disparition du véhicule",
  accident: "Accident, panne, assistance",
  security: "Sécurité du bénéficiaire",
  legal: "Réquisition d'une autorité",
  other: "Autre motif documenté",
};
export const GPS_STATUS_LABEL: Record<GpsGrantStatus, string> = {
  requested: "Demandée", approved: "Accordée", rejected: "Refusée", revoked: "Révoquée",
};
export const GPS_STATUS_TONE: Record<string, Tone> = { requested: "amber", approved: "green", rejected: "red", revoked: "slate" };
/** Durée maximale d'une exception (miroir de `gps.MAX_DURATION`). */
export const GPS_MAX_HOURS = 72;
/** Justification minimale (miroir de `gps.request_access`). */
export const GPS_MIN_REASON = 15;

export interface GpsAccessGrant {
  id: string;
  vehicle: string;
  vehicle_registration: string;
  assignment: string | null;
  assignment_reference: string | null;
  grantee: string;
  grantee_name: string | null;
  motive: GpsMotive;
  motive_display: string;
  reason: string;
  starts_at: string;
  ends_at: string;
  status: GpsGrantStatus;
  status_display: string;
  decided_by_name: string | null;
  decided_at: string | null;
  decision_note: string;
  revoked_at: string | null;
  last_used_at: string | null;
  use_count: number;
  created_at: string;
}

export function useGpsAccess(params: Record<string, string> = {}, enabled = true) {
  return useQuery({
    queryKey: [KEY, "gps-access", params],
    enabled,
    retry,
    queryFn: async () =>
      rows((await api.get<Paginated<GpsAccessGrant> | GpsAccessGrant[]>("/carplan/gps-access/", { params: { page_size: "100", ...params } })).data),
  });
}

export function useRequestGpsAccess() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async (body: { vehicle: string; motive: GpsMotive; reason: string; starts_at: string; ends_at: string }) =>
      (await api.post<GpsAccessGrant>("/carplan/gps-access/", body)).data,
    onSuccess: invalidate,
  });
}

export function useGpsAccessAction() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ id, action, note }: { id: string; action: "approve" | "reject" | "revoke"; note?: string }) =>
      (await api.post<GpsAccessGrant>(`/carplan/gps-access/${id}/${action}/`, action === "revoke" ? {} : { note: note ?? "" })).data,
    onSuccess: invalidate,
  });
}

// --- Self-service « Mon véhicule » -------------------------------------------------------------

export function useMyVehicle(enabled = true) {
  return useQuery({
    queryKey: [ME, "vehicle"],
    enabled,
    retry,
    queryFn: async () => (await api.get<MyVehicle>("/carplan/me/")).data,
  });
}

function useMine<T>(sub: string, enabled = true) {
  return useQuery({
    queryKey: [ME, sub],
    enabled,
    retry,
    queryFn: async () => rows((await api.get<T[] | Paginated<T>>(`/carplan/me/${sub}/`)).data),
  });
}

export const useMyInspections = (enabled = true) => useMine<Inspection>("inspections", enabled);
export const useMyMileage = (enabled = true) => useMine<MileageReading>("mileage", enabled);
export const useMyRequests = (enabled = true) => useMine<CarPlanRequest>("requests", enabled);
export const useMyIncidents = (enabled = true) => useMine<CarPlanIncident>("incidents", enabled);
export const useMyHistory = (enabled = true) => useMine<MyAssignment>("history", enabled);

export function useDeclareMyMileage() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async (body: { odometer: number; reading_date?: string; professional_km?: number; private_km?: number }) =>
      (await api.post<MileageReading>("/carplan/me/mileage/", body)).data,
    onSuccess: invalidate,
  });
}

export function useMyTracking(enabled = true) {
  return useQuery({
    queryKey: [ME, "tracking"],
    enabled,
    retry,
    queryFn: async () => (await api.get<Tracking>("/carplan/me/tracking/")).data,
  });
}

/** Le bénéficiaire corrige SA dernière déclaration (48 h) : l'original reste tracé. */
export function useCorrectMyMileage() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ readingId, odometer, reason }: { readingId: number; odometer: number; reason?: string }) =>
      (await api.post<MileageReading>(`/carplan/me/mileage/${readingId}/correct/`,
        reason ? { odometer, reason } : { odometer })).data,
    onSuccess: invalidate,
  });
}

export function useCreateMyRequest() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async (body: { kind: string; description: string; desired_date?: string }) =>
      (await api.post<CarPlanRequest>("/carplan/me/requests/", body)).data,
    onSuccess: invalidate,
  });
}

export function useDeclareMyIncident() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async (input: {
      kind: string; occurred_at: string; description: string; location?: string; vehicle_drivable: boolean; photo?: File | null;
    }) => {
      const form = new FormData();
      form.append("kind", input.kind);
      form.append("occurred_at", input.occurred_at);
      form.append("description", input.description);
      if (input.location) form.append("location", input.location);
      form.append("vehicle_drivable", input.vehicle_drivable ? "true" : "false");
      if (input.photo) form.append("photo", input.photo);
      return (await api.post<CarPlanIncident>("/carplan/me/incidents/", form)).data;
    },
    onSuccess: invalidate,
  });
}
