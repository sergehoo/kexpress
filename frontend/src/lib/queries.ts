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
  Employee,
  Expense,
  FuelLog,
  Incident,
  KbotAnswer,
  MaintenanceRecord,
  NearbyVehicle,
  NotificationItem,
  Paginated,
  Reservation,
  Subsidiary,
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
    mutationFn: async (question: string) => {
      const { data } = await api.post<KbotAnswer>("/kbot/ask/", { question });
      return data;
    },
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
  purpose: string;
  passengers: number;
  needs_driver: boolean;
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
