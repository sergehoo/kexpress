"use client";

import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import { api } from "@/lib/api";
import type {
  AlertsResponse,
  AuditEntry,
  Driver,
  DriverMission,
  ElectricCharge,
  Employee,
  Expense,
  FuelLog,
  Incident,
  KbotResponse,
  LiveAlertsResponse,
  MaintenanceRecord,
  NearbyVehicle,
  NotificationItem,
  Paginated,
  Reservation,
  Subsidiary,
  SubsidiaryStats,
  SuggestedVehicle,
  Trip,
  TripRouteData,
  Vehicle,
  VehiclePosition,
} from "@/lib/types";

// --- Dashboard décisionnel ----------------------------------------------

export interface DecisionStats {
  period: { key: string; start: string; end: string };
  reservations: {
    total: number; validated: number; rejected: number; cancelled: number;
    pending: number; validation_rate: number | null; rejection_rate: number | null;
    processing_hours: number | null;
  };
  activity: {
    km: number; trips_done: number; usage_hours: number | null;
    late_returns: number; incidents: number;
  };
  fuel: {
    estimated_l: number; real_l: number; gap_pct: number | null;
    estimated_cost: number; real_cost: number;
  };
  cost: {
    total: number; general: number; fuel: number; maintenance: number;
    per_trip: number | null; per_km: number | null;
    detail: { key: string; label: string; value: number }[];
  };
  series: {
    label: string; reservations: number; validated: number; rejected: number;
    cancelled: number; fuel_l: number; fuel_cost: number; km: number; cost: number;
  }[];
  by_subsidiary: {
    name: string; fuel_cost: number; expenses: number; maintenance: number;
    total_cost: number; reservations: number; km: number; fuel_l: number;
  }[];
  top_vehicles_cost: { registration: string; cost: number }[];
  top_trips_cost: { trip_id: string; destination: string; cost: number }[];
  maintenance: {
    total_cost: number; count: number; breakdown_count: number;
    preventive_cost: number; corrective_cost: number;
    preventive_count: number; corrective_count: number;
    downtime_total_h: number; downtime_avg_h: number; immobilization_rate: number;
    top_breakdowns: { name: string; count: number; cost: number }[];
    top_cost_vehicles: { registration: string; cost: number }[];
    top_downtime_vehicles: { registration: string; hours: number }[];
    cancelled_due_to_breakdown: number;
  };
  compliance: {
    vehicles_total: number; compliant: number; non_compliant: number; rate: number;
    issues: Record<string, number>;
    insurances_to_renew: number; inspections_to_renew: number;
    revisions_due: number; revisions_overdue: number;
    annual_insurance_cost: number; annual_inspection_cost: number; annual_revision_cost: number;
  };
  scope: string;
  subsidiary_name: string | null;
  /** false : profil sans droit financier — l'API a retiré tous les montants. */
  costs_visible?: boolean;
}

export function useDashboardStats(params: Record<string, string>) {
  return useQuery({
    queryKey: ["dashboard-stats", params],
    queryFn: async () => {
      const { data } = await api.get<DecisionStats>("/dashboard/stats/", { params });
      return data;
    },
  });
}

/** Occupation d'un véhicule et répartition de son kilométrage sur la période (§10). */
export interface VehicleOccupancy {
  vehicle: string;
  registration: string;
  capacity: number;
  trips: number;
  hours_in_mission: number;
  hours_available: number;
  temporal_rate: number | null;
  passengers_carried: number;
  seats_offered: number;
  fill_rate: number | null;
  passengers_per_trip: number | null;
  /** Exige les missions regroupées : `null` tant qu'elles n'existent pas (≠ 0 %). */
  mutualisation_rate: number | null;
  total_km: number;
  loaded_km: number;
  empty_km: number;
  loaded_rate: number | null;
  empty_rate: number | null;
}

export interface MutualisationStats {
  trips: number;
  grouped_trips: number;
  missions: number;
  /** `null` = aucune course sur la période (≠ 0 %, qui signifie « rien de mutualisé »). */
  rate: number | null;
  trips_per_mission: number | null;
}

export interface OccupancyStats {
  mutualisation: MutualisationStats;
  period: string;
  start: string;
  end: string;
  results: VehicleOccupancy[];
  fleet: {
    total_km: number;
    loaded_km: number;
    empty_km: number;
    loaded_rate: number | null;
    empty_rate: number | null;
  };
}

export function useOccupancyStats(params: Record<string, string>) {
  return useQuery({
    queryKey: ["dashboard-occupancy", params],
    queryFn: async () => {
      const { data } = await api.get<OccupancyStats>("/dashboard/occupancy/", { params });
      return data;
    },
  });
}

export function useSubsidiaries() {
  return useQuery({
    queryKey: ["subsidiaries"],
    queryFn: async () => {
      const { data } = await api.get<Paginated<Subsidiary>>("/subsidiaries/");
      return data.results;
    },
    staleTime: 5 * 60_000,
  });
}

export function useSubsidiary(id?: string | null) {
  return useQuery({
    queryKey: ["subsidiary", id],
    enabled: !!id,
    queryFn: async () => {
      const { data } = await api.get<Subsidiary>(`/subsidiaries/${id}/`);
      return data;
    },
  });
}

export function useSubsidiaryStats(id?: string | null) {
  return useQuery({
    queryKey: ["subsidiary-stats", id],
    enabled: !!id,
    queryFn: async () => {
      const { data } = await api.get<SubsidiaryStats>(`/subsidiaries/${id}/stats/`);
      return data;
    },
    staleTime: 60_000,
  });
}

export function useNotifications() {
  return useQuery({
    queryKey: ["notifications"],
    queryFn: async () => {
      const { data } = await api.get<Paginated<NotificationItem>>("/notifications/", {
        params: { page_size: "20" },
      });
      return data.results;
    },
    refetchInterval: 60_000,
  });
}

export function useUnreadCount() {
  return useQuery({
    queryKey: ["notifications", "unread"],
    queryFn: async () => {
      const { data } = await api.get<{ count: number }>("/notifications/unread_count/");
      return data.count;
    },
    refetchInterval: 60_000,
  });
}

export function useMarkAllRead() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      await api.post("/notifications/mark_all_read/", {});
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["notifications"] }),
  });
}

export function useKbot() {
  return useMutation({
    mutationFn: async (vars: { message: string; page?: string; lat?: number; lng?: number }) => {
      const { data } = await api.post<KbotResponse>("/kbot/chat/", {
        message: vars.message,
        context: { page: vars.page },
        lat: vars.lat,
        lng: vars.lng,
      });
      return data;
    },
  });
}

export function useKbotSuggestions(page?: string) {
  return useQuery({
    queryKey: ["kbot-suggestions", page ?? "default"],
    queryFn: async () => {
      const { data } = await api.get<{ suggestions: string[] }>("/kbot/suggestions/", {
        params: page ? { page } : {},
      });
      return data.suggestions;
    },
    staleTime: 10 * 60_000,
  });
}

// --- Véhicules ----------------------------------------------------------

export function useVehicles(params: Record<string, string> = {}) {
  return useQuery({
    queryKey: ["vehicles", params],
    queryFn: async () => {
      const { data } = await api.get<Paginated<Vehicle>>("/vehicles/", { params });
      return data;
    },
  });
}

export function useVehicle(id?: string | null) {
  return useQuery({
    queryKey: ["vehicle", id],
    enabled: !!id,
    queryFn: async () => {
      const { data } = await api.get<Vehicle>(`/vehicles/${id}/`);
      return data;
    },
  });
}

// --- Conformité véhicule (assurance / visite / révision) -----------------

export interface InsuranceItem {
  id: string; vehicle: string; company: string; policy_number: string;
  start_date: string | null; expiry_date: string; cost: string | null;
}
export interface InspectionItem {
  id: string; vehicle: string; last_date: string | null; next_date: string;
  center: string; result: string; result_display: string; cost: string | null;
  observations: string;
}
export interface RevisionItem {
  id: string; vehicle: string; date: string; mileage_at_revision: number;
  cost: string | null; provider: string; notes: string;
}

function useVehicleSub<T>(resource: string, vehicleId?: string | null) {
  return useQuery({
    queryKey: [resource, vehicleId],
    enabled: !!vehicleId,
    queryFn: async () => {
      const { data } = await api.get<Paginated<T>>(`/${resource}/`, {
        params: { vehicle: vehicleId, page_size: "50" },
      });
      return data.results;
    },
  });
}

export const useVehicleInsurances = (id?: string | null) => useVehicleSub<InsuranceItem>("vehicle-insurances", id);
export const useVehicleInspections = (id?: string | null) => useVehicleSub<InspectionItem>("vehicle-inspections", id);
export const useVehicleRevisions = (id?: string | null) => useVehicleSub<RevisionItem>("vehicle-revisions", id);

// --- Chauffeurs ---------------------------------------------------------

export function useDrivers(params: Record<string, string> = {}) {
  return useQuery({
    queryKey: ["drivers", params],
    queryFn: async () => {
      const { data } = await api.get<Paginated<Driver>>("/drivers/", { params });
      return data;
    },
  });
}

// --- Employés / Audit / Alertes / Dépenses -----------------------------

export function useEmployees(params: Record<string, string> = {}) {
  return useQuery({
    queryKey: ["employees", params],
    queryFn: async () => {
      const { data } = await api.get<Paginated<Employee>>("/employees/", { params });
      return data;
    },
  });
}

export function useAudit(params: Record<string, string> = {}) {
  return useQuery({
    queryKey: ["audit", params],
    queryFn: async () => {
      const { data } = await api.get<Paginated<AuditEntry>>("/audit/", { params });
      return data;
    },
  });
}

export function useAlerts() {
  return useQuery({
    queryKey: ["alerts"],
    queryFn: async () => {
      const { data } = await api.get<AlertsResponse>("/alerts/");
      return data;
    },
    refetchInterval: 60_000,
  });
}

export function useIncidents() {
  return useQuery({
    queryKey: ["incidents"],
    queryFn: async () => {
      const { data } = await api.get<{ count: number; results: Incident[] }>("/incidents/");
      return data.results;
    },
  });
}

export function useExpenses(params: Record<string, string> = {}) {
  return useQuery({
    queryKey: ["expenses", params],
    queryFn: async () => {
      const { data } = await api.get<Paginated<Expense>>("/expenses/", { params });
      return data;
    },
  });
}

// --- Maintenance / Carburant -------------------------------------------

export function useMaintenance(params: Record<string, string> = {}) {
  return useQuery({
    queryKey: ["maintenance", params],
    queryFn: async () => {
      const { data } = await api.get<Paginated<MaintenanceRecord>>("/maintenance/", { params });
      return data;
    },
  });
}

export function useBreakdownTypes() {
  return useQuery({
    queryKey: ["breakdown-types"],
    queryFn: async () => {
      const { data } = await api.get<Paginated<{ id: string; name: string }>>("/breakdown-types/", {
        params: { page_size: "50" },
      });
      return data.results;
    },
    staleTime: 5 * 60_000,
  });
}

export interface MaintenanceForecast {
  vehicle: string;
  registration: string;
  subsidiary_name: string;
  mileage: number;
  km_per_day: number;
  next_revision_km: number;
  revision_remaining_km: number;
  days_to_revision: number | null;
  revision_eta: string | null;
  breakdowns_180d: number;
  breakdown_risk: string;
  next_breakdown_km_estimate: number | null;
}

export function useMaintenanceForecast(enabled = true) {
  return useQuery({
    queryKey: ["maintenance-forecast"],
    enabled,
    staleTime: 5 * 60_000,
    queryFn: async () => {
      const { data } = await api.get<{ count: number; results: MaintenanceForecast[]; note: string }>(
        "/maintenance-forecast/",
      );
      return data;
    },
  });
}

export function useMaintenanceTypes() {
  return useQuery({
    queryKey: ["maintenance-types"],
    queryFn: async () => {
      const { data } = await api.get<Paginated<{ id: string; name: string }>>("/maintenance-types/", {
        params: { page_size: "50" },
      });
      return data.results;
    },
    staleTime: 5 * 60_000,
  });
}

export function useFuel(params: Record<string, string> = {}) {
  return useQuery({
    queryKey: ["fuel", params],
    queryFn: async () => {
      const { data } = await api.get<Paginated<FuelLog>>("/fuel/", { params });
      return data;
    },
  });
}

/** Recharges électriques (section Électricité de la gestion de l'énergie). */
export function useElectricCharges(params: Record<string, string> = {}) {
  return useQuery({
    queryKey: ["electric-charges", params],
    queryFn: async () => {
      const { data } = await api.get<Paginated<ElectricCharge>>("/electric-charges/", { params });
      return data;
    },
  });
}

// --- Itinéraire d'une course (prévu vs réel) ---------------------------

export function useTripRoute(tripId?: string | null) {
  return useQuery({
    queryKey: ["trip-route", tripId],
    enabled: !!tripId,
    refetchInterval: 5_000,
    queryFn: async () => {
      const { data } = await api.get<TripRouteData>(`/tracking/trips/${tripId}/route/`);
      return data;
    },
  });
}

// --- Relecture d'itinéraire (trace GPS horodatée) ----------------------

export interface TripReplayData {
  trip_id: string;
  destination: string;
  vehicle_registration: string | null;
  planned: [number, number][];
  points: [number, number, string, number | null][]; // [lat, lng, ISO, speed]
  distance_km: number;
  started_at: string | null;
  ended_at: string | null;
}

export function useTripReplay(tripId?: string | null, enabled = false) {
  return useQuery({
    queryKey: ["trip-replay", tripId],
    enabled: !!tripId && enabled,
    queryFn: async () => {
      const { data } = await api.get<TripReplayData>(`/tracking/trips/${tripId}/replay/`);
      return data;
    },
  });
}

// --- Course active de l'utilisateur ------------------------------------

export function useActiveTrip() {
  return useQuery({
    queryKey: ["active-trip"],
    queryFn: async () => {
      const { data } = await api.get<{ trip: Trip | null }>("/trips/active/");
      return data.trip;
    },
    refetchInterval: 20_000,
  });
}

/** Missions du chauffeur connecté (planifiées / en cours / revenues) — espace chauffeur. */
export function useDriverMissions(enabled = true) {
  return useQuery({
    queryKey: ["driver-missions"],
    enabled,
    queryFn: async () => {
      const { data } = await api.get<{ results: DriverMission[] }>("/trips/my-missions/");
      return data.results;
    },
    refetchInterval: 20_000,
  });
}

// --- Carte : véhicules proches -----------------------------------------

export function useNearbyVehicles(lat?: number, lng?: number) {
  return useQuery({
    queryKey: ["nearby-vehicles", lat, lng],
    enabled: lat != null && lng != null,
    refetchInterval: 15_000,
    queryFn: async () => {
      const { data } = await api.get<{ count: number; results: NearbyVehicle[]; suggestion: string }>(
        "/map/nearby-vehicles/",
        { params: { lat, lng } },
      );
      return data;
    },
  });
}

// --- Fleet Fuel Intelligence (gestionnaires) -----------------------------

export interface FuelIntelData {
  day: { liters: number; cost: number };
  month: { liters: number; cost: number };
  forecast: { liters: number; cost: number };
  fleet_rate: number | null;
  gap_pct: number | null;
  top_vehicles: { label: string; rate: number; samples: number }[];
  top_drivers: { label: string; rate: number; samples: number }[];
  subsidiaries: { label: string; rate: number; samples: number }[];
  overconsumption: { label: string; rate: number; fleet_rate: number; excess_pct: number }[];
  prices: Record<string, { label: string; price: number | null; date: string | null; history: { price: number; date: string }[] }>;
}

export function useFuelIntel(enabled = true) {
  return useQuery({
    queryKey: ["fuel-intel"],
    enabled,
    refetchInterval: 120_000,
    queryFn: async () => {
      const { data } = await api.get<FuelIntelData>("/fuel-intel/");
      return data;
    },
  });
}

// --- Zones de géofencing -------------------------------------------------

export interface GeofenceZoneData {
  id: string;
  name: string;
  zone_type: string;
  zone_type_display: string;
  polygon: [number, number][];
  /** Zone opérationnelle (§3) : identification stable et définition par rayon.
   *  Une telle zone a un `center` + `radius_m` au lieu d'un `polygon`. */
  code: string;
  category: string;
  center: [number, number] | null;
  radius_m: number | null;
}

export function useGeofenceZones() {
  return useQuery({
    queryKey: ["geofence-zones"],
    queryFn: async () => {
      const { data } = await api.get<{ results: GeofenceZoneData[] }>("/tracking/zones/");
      return data.results;
    },
    staleTime: 5 * 60_000,
  });
}

// --- Anomalies opérationnelles live (centre de contrôle #3F) -----------

export function useLiveAlerts(enabled = true) {
  return useQuery({
    queryKey: ["live-alerts"],
    enabled,
    queryFn: async () => {
      const { data } = await api.get<LiveAlertsResponse>("/tracking/live-alerts/");
      return data;
    },
    refetchInterval: 15_000,
    refetchIntervalInBackground: false,
  });
}

// --- Positions flotte (carte temps réel) -------------------------------

export function useFleetPositions(subsidiaryId?: string, enabled = true) {
  const params = subsidiaryId ? { subsidiary: subsidiaryId } : {};
  return useQuery({
    queryKey: ["fleet-positions", subsidiaryId ?? "all"],
    enabled,
    queryFn: async () => {
      const { data } = await api.get<{ count: number; results: VehiclePosition[] }>(
        "/tracking/positions/",
        { params },
      );
      return data.results;
    },
    refetchInterval: 6_000,
    refetchIntervalInBackground: false,
  });
}

// --- Réservations -------------------------------------------------------

export function useReservations(params: Record<string, string> = {}) {
  return useQuery({
    queryKey: ["reservations", params],
    queryFn: async () => {
      const { data } = await api.get<Paginated<Reservation>>("/reservations/", { params });
      return data;
    },
  });
}

export interface CreateReservationInput {
  trip_date: string;
  departure_time: string;
  estimated_return: string;
  origin?: string;
  destination: string;
  trip_type?: "one_way" | "round_trip";
  return_time?: string | null;
  purpose: string;
  passengers: number;
  needs_driver: boolean;
  /** Décalage de départ accepté (min) : 0 = ferme. Débloque le partage de véhicule. */
  flexibility_minutes?: number;
  priority: string;
  subsidiary?: string;
  requester?: string;
}

export function useCreateReservation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (input: CreateReservationInput) => {
      const { data } = await api.post<Reservation>("/reservations/", input);
      return data;
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["reservations"] }),
  });
}

export function useReservation(id?: string | null) {
  return useQuery({
    queryKey: ["reservation", id],
    enabled: !!id,
    queryFn: async () => {
      const { data } = await api.get<Reservation>(`/reservations/${id}/`);
      return data;
    },
  });
}

type ActionBody = Record<string, unknown> | undefined;

/** Action générique du workflow réservation (submit/approve/reject/cancel/assign-*). */
export function useReservationAction(action: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, body }: { id: string; body?: ActionBody }) => {
      const { data } = await api.post<Reservation>(`/reservations/${id}/${action}/`, body ?? {});
      return data;
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["reservations"] });
      qc.invalidateQueries({ queryKey: ["reservation"] });
      qc.invalidateQueries({ queryKey: ["vehicles"] });
      qc.invalidateQueries({ queryKey: ["trips"] });
      qc.invalidateQueries({ queryKey: ["trip"] });
    },
  });
}

// --- Courses ------------------------------------------------------------

export function useTrips(params: Record<string, string> = {}) {
  return useQuery({
    queryKey: ["trips", params],
    queryFn: async () => {
      const { data } = await api.get<Paginated<Trip>>("/trips/", { params });
      return data;
    },
  });
}

export function useTrip(id?: string | null) {
  return useQuery({
    queryKey: ["trip", id],
    enabled: !!id,
    refetchInterval: 15_000,
    queryFn: async () => {
      const { data } = await api.get<Trip>(`/trips/${id}/`);
      return data;
    },
  });
}

/** Dispatching par segment : véhicules dispo classés par proximité (ETA) du point de
 *  départ de la course. Alimente le modal d'affectation par course. */
export function useTripSuggestVehicle(tripId?: string | null, enabled = true) {
  return useQuery({
    queryKey: ["trip-suggest-vehicle", tripId],
    enabled: !!tripId && enabled,
    queryFn: async () => {
      const { data } = await api.get<{ results: SuggestedVehicle[] }>(
        `/trips/${tripId}/suggest-vehicle/`,
      );
      return data.results;
    },
  });
}

export function useTripAction(action: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, body }: { id: string; body?: ActionBody }) => {
      const { data } = await api.post<Trip>(`/trips/${id}/${action}/`, body ?? {});
      return data;
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["trips"] });
      qc.invalidateQueries({ queryKey: ["trip"] });
      qc.invalidateQueries({ queryKey: ["reservations"] });
      qc.invalidateQueries({ queryKey: ["reservation"] });
      qc.invalidateQueries({ queryKey: ["vehicles"] });
    },
  });
}

// --- Centre de dispatching (§4) -----------------------------------------

export interface BoardTrip {
  id: string;
  destination: string;
  leg: string;
  status: string;
  status_display: string;
  subsidiary_name: string | null;
  passengers: number | null;
  priority: string | null;
  planned_departure_at: string | null;
  planned_arrival_at: string | null;
  vehicle: string | null;
  vehicle_registration: string | null;
  driver_name: string | null;
  origin_zone_name: string | null;
  destination_zone_name: string | null;
  grouped: boolean;
  distance_km: string | null;
  /** Présent seulement pour les profils `finance.view_trip_cost` (absent sinon). */
  pricing?: TripPricingBlock | null;
}

export interface TripPricingBlock {
  amount_per_km: string | null;
  currency: string;
  estimated_cost: string | null;
  actual_cost: string | null;
}

export interface ZoneMatrixCell {
  origin_zone_name: string;
  destination_zone_name: string;
  trips: number;
  passengers: number;
  unassigned: number;
}

export interface BoardMission {
  id: string;
  code: string;
  status: string;
  status_display: string;
  vehicle_registration: string;
  vehicle_capacity: number;
  driver_name: string | null;
  planned_departure_at: string | null;
  trips: number;
}

export interface DispatchBoard {
  window: { start: string; end: string };
  trips: BoardTrip[];
  unassigned: BoardTrip[];
  zone_matrix: ZoneMatrixCell[];
  missions: BoardMission[];
  available_vehicles: {
    id: string; registration: string; label: string; capacity: number;
    fuel_type: string; subsidiary_name: string | null;
  }[];
  pending_suggestions: number;
  totals: { trips: number; unassigned: number; passengers: number; grouped: number };
}

export function useDispatchBoard(params: Record<string, string> = {}) {
  return useQuery({
    queryKey: ["dispatch-board", params],
    queryFn: async () => {
      const { data } = await api.get<DispatchBoard>("/dispatch/board/", { params });
      return data;
    },
    refetchInterval: 60_000,
  });
}

/** Proposition du moteur de dispatching — LECTURE. Rien ne s'applique sans décision. */
export interface DispatchSuggestion {
  id: string;
  kind: string;
  kind_display: string;
  payload: { trip_ids: string[]; capacity_required?: number };
  metrics: Record<string, unknown>;
  rationale: string;
  score: number;
  rank: number;
  status: string;
  status_display: string;
  created_at: string;
  /** Sans / avec mutualisation — profils `finance.view_trip_cost` uniquement. */
  financial_impact?: PoolingImpact | null;
}

export interface PoolingImpact {
  km_separate: string;
  km_grouped: string;
  km_avoided: string;
  cost_separate: string | null;
  cost_grouped: string | null;
  saving: string | null;
  amount_per_km: string | null;
  currency: string;
  distance_source: string;
  /** true : détour mesuré à vol d'oiseau puis corrigé — une estimation. */
  approximate: boolean;
}

export function useDispatchSuggestions() {
  return useQuery({
    queryKey: ["dispatch-suggestions"],
    queryFn: async () => {
      const { data } = await api.get<Paginated<DispatchSuggestion>>("/dispatch-suggestions/", {
        params: { status: "proposed" },
      });
      return data.results;
    },
  });
}

export function useGenerateSuggestions() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const { data } = await api.post<DispatchSuggestion[]>("/dispatch-suggestions/generate/", {});
      return data;
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dispatch-suggestions"] });
      qc.invalidateQueries({ queryKey: ["dispatch-board"] });
    },
  });
}

/** Décision humaine sur une suggestion (§9) : accepter, modifier, rejeter. */
export function useDecideSuggestion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (vars: {
      id: string; action: "accept" | "modify" | "reject";
      vehicle?: string; driver?: string; comment?: string;
    }) => {
      const { id, ...body } = vars;
      const { data } = await api.post(`/dispatch-suggestions/${id}/decide/`, body);
      return data;
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dispatch-suggestions"] });
      qc.invalidateQueries({ queryKey: ["dispatch-board"] });
      qc.invalidateQueries({ queryKey: ["missions"] });
      qc.invalidateQueries({ queryKey: ["trips"] });
    },
  });
}

/** Potentiel de mutualisation sur une période passée (simulation contrefactuelle).
 *
 *  `assumptions` accompagne TOUJOURS le chiffre : un gain contrefactuel n'est pas une mesure
 *  et ne doit jamais être présenté comme telle. */
export interface MutualisationPotential {
  period: string;
  start: string;
  end: string;
  trips_examined: number;
  groupings: number;
  trips_groupable: number;
  km_avoided: number;
  /** Ventilé par unité : litres et kWh ne s'additionnent pas. */
  energy_avoided: Record<string, number>;
  cost_avoided: number;
  co2_avoided_kg: number;
  assumptions: string[];
}

export function useMutualisationPotential(params: Record<string, string> = {}) {
  return useQuery({
    queryKey: ["dispatch-potential", params],
    queryFn: async () => {
      const { data } = await api.get<MutualisationPotential>("/dispatch/potential/", { params });
      return data;
    },
  });
}

/** Efficacité énergétique comparable (§16).
 *
 *  Le PASSAGER-kilomètre est l'indicateur d'arbitrage : au seul kilomètre, un minibus plein
 *  paraît moins efficace qu'une berline vide, ce qui conduirait à renouveler la flotte à
 *  contresens. `unit` accompagne toujours `quantity` — litres et kWh ne se mélangent pas.
 *
 *  Les ratios valent `null` quand il n'y a pas de base de comparaison (véhicule à l'arrêt,
 *  course sans passager) : afficher 0 le ferait passer pour le plus économe de la flotte. */
export interface VehicleEfficiency {
  vehicle: string;
  registration: string;
  fuel_type: string;
  capacity: number;
  /** Pleins ET recharges sur la période : la quantité ne porte alors qu'une seule unité. */
  mixed_energy: boolean;
  unit: string;
  quantity: number;
  cost: number | null;
  km: number;
  passenger_km: number;
  trips: number;
  energy_per_km: number | null;
  energy_per_passenger_km: number | null;
  cost_per_km: number | null;
  cost_per_passenger_km: number | null;
  cost_per_trip: number | null;
  co2_kg: number | null;
  co2_per_passenger_km: number | null;
}

export interface EnergyEfficiencyData {
  period: string;
  start: string;
  end: string;
  /** Classés du MOINS au plus efficace : ce sont les premiers qu'on arbitre. */
  results: VehicleEfficiency[];
  fleet: {
    /** Ventilé par unité — jamais un total unique d'énergie. */
    quantities: Record<string, number>;
    cost: number;
    km: number;
    passenger_km: number;
    trips: number;
    co2_kg: number | null;
    cost_per_km: number | null;
    cost_per_passenger_km: number | null;
    cost_per_trip: number | null;
  };
}

export function useEnergyEfficiency(params: Record<string, string> = {}) {
  return useQuery({
    queryKey: ["energy-efficiency", params],
    queryFn: async () => {
      const { data } = await api.get<EnergyEfficiencyData>("/energy/efficiency/", { params });
      return data;
    },
  });
}

/** Dispatching anticipatif (§ demandes récurrentes).
 *
 *  Le backend détecte les trajets qui reviennent chaque semaine (même origine/destination,
 *  même jour, même heure) et signale ceux dont la prochaine occurrence n'a PAS encore de
 *  réservation. Rien n'est créé automatiquement : le motif est une information, la décision
 *  reste au dispatcher (§9). */
export interface AnticipationPattern {
  origin: string;
  destination: string;
  weekday: number;
  weekday_label: string;
  time: string;
  occurrences: number;
  weeks_seen: number;
  weeks_observed: number;
  /** Part des semaines observées où la demande s'est produite (0-1). */
  regularity: number;
  avg_passengers: number;
  next_expected: string;
  /** true si une réservation existe déjà à ±90 min de l'heure attendue. */
  covered: boolean;
}

export interface AnticipationData {
  window: { start: string; end: string; weeks: number };
  patterns: AnticipationPattern[];
  to_anticipate: number;
  assumptions: string[];
}

export function useDispatchAnticipation(params: Record<string, string> = {}) {
  return useQuery({
    queryKey: ["dispatch-anticipation", params],
    queryFn: async () => {
      const { data } = await api.get<AnticipationData>("/dispatch/anticipation/", { params });
      return data;
    },
  });
}


// --- Finance & Coûts : barème kilométrique ------------------------------------

export interface TripPricingRule {
  id: string;
  name: string;
  amount_per_km: string;
  currency: string;
  valid_from: string;
  valid_until: string | null;
  scope: string;
  scope_display: string;
  active: boolean;
  description: string;
  reason: string;
  version: number;
  /** Réservé à qui gère le barème (null sinon). */
  trips_priced: number | null;
  created_by_name: string | null;
  updated_by_name: string | null;
  created_at: string;
  updated_at: string;
}

export function useTripPricingRules(enabled = true) {
  return useQuery({
    queryKey: ["trip-pricing-rules"],
    enabled,
    queryFn: async () => {
      const { data } = await api.get<Paginated<TripPricingRule>>("/finance/trip-pricing-rules/", {
        params: { page_size: 100, ordering: "-valid_from" },
      });
      return data.results;
    },
  });
}

export interface CostBucket { key: string; label: string; cost: string; trips: number; km: string }

export interface TripCostStats {
  period: { key: string; start: string; end: string };
  currency: string;
  realised: {
    trips: number; priced_trips: number; unpriced_trips: number;
    total_cost: string; km: string;
    avg_cost_per_trip: string | null; avg_cost_per_km: string | null;
  };
  planned: { trips: number; unpriced_trips: number; estimated_cost: string | null; until: string };
  by_subsidiary: CostBucket[];
  by_vehicle: CostBucket[];
  by_zone: CostBucket[];
  by_department: CostBucket[];
  by_requester: CostBucket[];
  series: { label: string; cost: string }[];
  pooled: {
    cost: string; km_avoided: string; saving: string | null;
    missions_measured: number; missions_unmeasured: number; approximate: boolean;
  };
  /** `cost` null : aucun km à vide valorisable (non valorisé, et non gratuit). */
  empty_km: { km: string; cost: string | null; unpriced_km: string; partial: boolean };
  tariff_vs_operating: { tariff_cost: string; energy_cost: string; gap: string; scope: string };
  assumptions: string[];
}

export function useTripCostStats(params: Record<string, string>, enabled = true) {
  return useQuery({
    queryKey: ["trip-cost-stats", params],
    enabled,
    queryFn: async () => {
      const { data } = await api.get<TripCostStats>("/finance/trip-costs/", { params });
      return data;
    },
  });
}


// --- Finance F1 : coût réel -------------------------------------------------------------
// Montants en texte (Decimal côté API) ; null = INCONNU, jamais 0.

export type Money = string | null;

export interface FinancePeriod {
  period: string; // AAAA-MM
  status: "open" | "closed";
  closed_at: string | null;
  closed_by: string | null;
  can_close: boolean;
}

export interface SubsidiaryCosts {
  period: string;
  subsidiary: string | null;
  provisional: boolean;
  currency: string;
  expenses: Money;
  energy: Money;
  maintenance: Money;
  trips_cost: Money;
  trips_count: number;
  trips_incomplete: number;
  indirect_charges: Money;
  under_utilisation_cost: Money;
  legacy_to_reconcile: { count: number; amount: Money };
}

export interface VehicleCostRow {
  vehicle: string;
  registration: string;
  subsidiary: string;
  subsidiary_name: string | null;
  period: string;
  provisional: boolean;
  components: Record<"insurance" | "depreciation" | "maintenance" | "tyres" | "subscriptions" | "taxes" | "other_fixed", Money>;
  fixed_cost: Money;
  absorbed_cost: Money;
  unabsorbed_cost: Money;
  under_utilisation_cost: Money;
  utilisation_rate: Money;
  used_km: Money;
  normative_km: Money;
  empty_km: Money;
  energy_cost: Money;
  other_direct_cost: Money;
  total_cost: Money;
  cost_per_km: Money;
  cost_per_trip: Money;
  empty_cost: Money;
  loaded_cost: Money;
  trips: number;
  missing: string[];
}

export interface TripCostSheet {
  trip: string;
  destination?: string;
  vehicle?: string | null;
  departure?: string | null;
  currency: string;
  distance_km: Money;
  passengers: number | null;
  energy_cost: Money;
  energy_source: string;
  driver_cost: Money;
  tolls_cost: Money;
  parking_cost: Money;
  direct_expenses_cost: Money;
  maintenance_cost: Money;
  tyres_cost: Money;
  insurance_cost: Money;
  depreciation_cost: Money;
  other_charges_cost: Money;
  total_direct: Money;
  total_indirect: Money;
  full_cost: Money;
  cost_per_km: Money;
  cost_per_passenger: Money;
  cost_per_passenger_km: Money;
  status: "pending" | "direct_frozen" | "complete";
  missing: string[];
  provisional: boolean;
  tariff: { value: Money; basis: "actual" | "estimated" | null; amount_per_km: Money; frozen: boolean };
  gap: Money;
}

export interface CostCenter {
  id: string;
  subsidiary: string;
  subsidiary_name: string;
  code: string;
  name: string;
  kind: string;
  department: string | null;
  department_name: string | null;
  erp_code: string;
  active: boolean;
}

export interface VehicleCharge {
  id: string;
  vehicle: string;
  vehicle_registration: string;
  kind: string;
  kind_display: string;
  label: string;
  amount: string;
  period_start: string;
  period_end: string;
  supplier: string;
  notes: string;
}

export interface VehicleAcquisition {
  id: string;
  vehicle: string;
  mode: string;
  mode_display: string;
  acquisition_date: string;
  purchase_price: Money;
  residual_value: string;
  depreciation_months: number | null;
  monthly_payment: Money;
  normative_monthly_km: number | null;
  currency: string;
  notes: string;
}

export function useFinancePeriods(enabled = true) {
  return useQuery({
    queryKey: ["finance-periods"],
    enabled,
    queryFn: async () => {
      const { data } = await api.get<{ results: FinancePeriod[] }>("/finance/periods/");
      return data.results;
    },
  });
}

function periodQuery<T>(key: string, url: string, period: string, subsidiary: string, enabled: boolean) {
  const params: Record<string, string> = { period };
  if (subsidiary) params.subsidiary = subsidiary;
  return {
    queryKey: [key, params],
    enabled: enabled && !!period,
    queryFn: async () => {
      const { data } = await api.get<T>(url, { params });
      return data;
    },
  };
}

export function useSubsidiaryCosts(period: string, subsidiary = "", enabled = true) {
  return useQuery(periodQuery<SubsidiaryCosts>("subsidiary-costs", "/finance/subsidiary-costs/", period, subsidiary, enabled));
}

export function useVehicleCosts(period: string, subsidiary = "", enabled = true) {
  return useQuery(periodQuery<{ period: string; results: VehicleCostRow[] }>(
    "vehicle-costs", "/finance/vehicle-costs/", period, subsidiary, enabled));
}

export function useTripCostSheets(period: string, subsidiary = "", enabled = true) {
  return useQuery(periodQuery<{ period: string; results: TripCostSheet[] }>(
    "trip-cost-sheets", "/finance/trip-cost-sheets/", period, subsidiary, enabled));
}

export function useVehicleCost(vehicleId: string, period: string, enabled = true) {
  return useQuery({
    queryKey: ["vehicle-cost", vehicleId, period],
    enabled: enabled && !!vehicleId && !!period,
    queryFn: async () => {
      const { data } = await api.get<VehicleCostRow>(`/finance/vehicles/${vehicleId}/costs/`, { params: { period } });
      return data;
    },
  });
}

export function useCostCenters(enabled = true) {
  return useQuery({
    queryKey: ["finance/cost-centers"],
    enabled,
    queryFn: async () => {
      const { data } = await api.get<Paginated<CostCenter>>("/finance/cost-centers/", { params: { page_size: "200" } });
      return data.results;
    },
  });
}

export function useVehicleCharges(vehicleId: string, enabled = true) {
  return useQuery({
    queryKey: ["finance/vehicle-charges", vehicleId],
    enabled: enabled && !!vehicleId,
    queryFn: async () => {
      const { data } = await api.get<Paginated<VehicleCharge>>("/finance/vehicle-charges/", { params: { vehicle: vehicleId } });
      return data.results;
    },
  });
}

export function useVehicleAcquisition(vehicleId: string, enabled = true) {
  return useQuery({
    queryKey: ["finance/vehicle-acquisitions", vehicleId],
    enabled: enabled && !!vehicleId,
    queryFn: async () => {
      const { data } = await api.get<Paginated<VehicleAcquisition>>("/finance/vehicle-acquisitions/", { params: { vehicle: vehicleId } });
      return data.results[0] ?? null;
    },
  });
}

export function useClosePeriod() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (period: string) => {
      const { data } = await api.post("/finance/periods/", { period });
      return data;
    },
    onSuccess: () => {
      for (const key of ["finance-periods", "subsidiary-costs", "vehicle-costs", "trip-cost-sheets", "vehicle-cost"]) {
        qc.invalidateQueries({ queryKey: [key] });
      }
    },
  });
}
