// Types alignés sur les serializers DRF du backend.

export interface Me {
  id: string;
  email: string;
  first_name: string;
  last_name: string;
  full_name: string;
  phone: string;
  role: string;
  role_display: string;
  subsidiary: string | null;
  subsidiary_name: string | null;
  department: string | null;
  manager: string | null;
  is_active: boolean;
  has_company_scope: boolean;
  date_joined: string;
}

export interface Paginated<T> {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
}

export interface VehicleCompliance {
  compliant: boolean;
  issues: { code: string; label: string }[];
  insurance_expiry: string | null;
  insurance_days_left: number | null;
  inspection_next_date: string | null;
  inspection_days_left: number | null;
  revision_interval_km: number;
  next_revision_km: number;
  revision_remaining_km: number;
}

export interface Vehicle {
  id: string;
  registration: string;
  brand: string;
  model: string;
  vehicle_type: string;
  vehicle_type_display: string;
  capacity: number;
  mileage: number;
  revision_interval_km: number;
  fuel_type: string;
  fuel_type_display: string;
  status: string;
  status_display: string;
  purchase_date: string | null;
  purchase_value: string | null;
  photo: string | null;
  notes: string;
  subsidiary: string;
  subsidiary_name: string;
  compliance: VehicleCompliance;
  created_at: string;
  updated_at: string;
}

export interface Subsidiary {
  id: string;
  name: string;
  code: string;
  city: string;
  company: string;
  company_name: string;
  is_active: boolean;
}

export interface NotificationItem {
  id: string;
  notification_type: string;
  type_display: string;
  channel: string;
  severity: string;
  title: string;
  message: string;
  link: string;
  is_read: boolean;
  read_at: string | null;
  created_at: string;
}

export interface KbotAnswer {
  intent: string;
  answer: string;
  data: { label: string; value: string }[] | null;
}

export interface DashboardStats {
  fleet: Record<string, number>;
  courses: Record<string, number>;
  drivers: Record<string, number>;
  rates: Record<string, number>;
  totals: Record<string, number>;
  charts: {
    courses_per_day: { date: string; label: string; count: number }[];
    vehicles_by_status: { status: string; label: string; count: number }[];
    costs: { name: string; value: number }[];
  };
  by_subsidiary: { name: string; vehicles: number; courses: number; fuel_cost: number }[];
  top_vehicles: { registration: string; trips: number }[];
  scope: string;
  subsidiary_name: string | null;
}

export interface PlaceResult {
  label: string;
  lat: number | null;
  lng: number | null;
  internal: boolean;
}

export interface RouteEstimate {
  geometry: [number, number][];
  distance_km: number;
  duration_min: number;
  fuel_liters: number;
  energy_level: string;
  available_vehicles: number;
  // Gestionnaires uniquement (absents pour l'employé demandeur)
  fuel_cost?: number;
  fuel_price?: number;
  fuel_price_date?: string | null;
}

export interface TripFuelIntel {
  estimated_l: number | null;
  real_l: number | null;
  gap_pct: number | null;
  efficiency_score: number | null;
  fuel_cost: number | null;
  fuel_price: number | null;
  fuel_price_date: string | null;
}

export interface NearbyVehicle {
  id: string;
  registration: string;
  brand: string;
  model: string;
  vehicle_type_display: string;
  capacity: number;
  latitude: string;
  longitude: string;
  distance_km: number;
  eta_min: number;
  subsidiary_name: string;
}

export interface TripTracking {
  trip_id: string;
  status: string;
  status_display: string;
  destination: string;
  driver_name: string | null;
  vehicle: { registration: string; latitude: string | null; longitude: string | null; speed_kmh: string | null };
  eta_min: number;
  distance_km: number;
  traveled_km: number;
  remaining_km: number;
  progress: number;
  planned: [number, number][];
  actual: [number, number][];
  destination_point: [number, number] | null;
}

export interface VehiclePosition {
  id: string;
  registration: string;
  brand: string;
  model: string;
  status: string;
  status_display: string;
  subsidiary_name: string;
  driver_name: string | null;
  destination: string | null;
  trip_id: string | null;
  latitude: string | null;
  longitude: string | null;
  speed_kmh: string | null;
  heading: string | null;
  recorded_at: string | null;
  is_late: boolean;
}

export interface TripRouteData {
  trip_id: string;
  destination: string;
  destination_point: [number, number] | null;
  planned: [number, number][];
  actual: [number, number][];
  distance_km: number;
  traveled_km: number;
  remaining_km: number;
  duration_min: number | null;
  progress: number;
  speed_kmh: number | null;
  driver_name: string | null;
}

export interface MaintenanceRecord {
  id: string;
  vehicle: string;
  vehicle_registration: string;
  maintenance_type: string;
  type_name: string;
  nature: string;
  nature_display: string;
  breakdown_type: string | null;
  breakdown_name: string | null;
  trip: string | null;
  trip_destination: string | null;
  status: string;
  status_display: string;
  declared_date: string | null;
  scheduled_date: string | null;
  performed_date: string | null;
  mileage: number | null;
  labor_cost: string | null;
  parts_cost: string | null;
  cost: string | null;
  provider: string;
  downtime_start: string | null;
  downtime_end: string | null;
  downtime_hours: number | null;
  validated_by: string | null;
  validated_by_name: string | null;
  notes: string;
  subsidiary: string;
  subsidiary_name: string;
  created_at: string;
}

export interface FuelLog {
  id: string;
  vehicle: string;
  vehicle_registration: string;
  date: string;
  liters: string;
  amount: string;
  price_per_liter: string | null;
  mileage: number | null;
  subsidiary_name: string;
}

export interface Employee {
  id: string;
  email: string;
  first_name: string;
  last_name: string;
  full_name: string;
  phone: string;
  role: string;
  role_display: string;
  subsidiary: string | null;
  subsidiary_name: string | null;
  is_active: boolean;
  date_joined: string;
}

export interface AuditEntry {
  id: string;
  created_at: string;
  action: string;
  action_display: string;
  actor_email: string | null;
  actor_name: string | null;
  target_repr: string;
  changes: Record<string, unknown>;
  ip_address: string | null;
}

export interface AlertItem {
  type: string;
  severity: string;
  title: string;
  detail: string;
  date: string;
}

export interface AlertsResponse {
  counts: { critical: number; warning: number; total: number };
  results: AlertItem[];
}

export interface Incident {
  id: string;
  kind: string;
  kind_display: string;
  severity: string;
  severity_display: string;
  subject: string;
  description: string;
  occurred_at: string | null;
}

export interface Expense {
  id: string;
  vehicle: string | null;
  vehicle_registration: string | null;
  trip: string | null;
  category: string;
  category_display: string;
  label: string;
  amount: string;
  date: string;
  subsidiary_name: string;
  created_at: string;
}

export interface Driver {
  id: string;
  first_name: string;
  last_name: string;
  full_name: string;
  phone: string;
  email: string;
  license_number: string;
  license_category: string;
  license_expiry: string | null;
  is_available: boolean;
  rating: string | null;
  subsidiary: string;
  subsidiary_name: string;
}

export interface ReservationValidation {
  id: string;
  level: string;
  level_display: string;
  validator: string | null;
  validator_name: string | null;
  decision: string;
  decision_display: string;
  comment: string;
  decided_at: string | null;
}

export interface Reservation {
  id: string;
  requester: string;
  requester_name: string;
  subsidiary: string;
  subsidiary_name: string;
  trip_date: string;
  departure_time: string;
  estimated_return: string;
  origin: string;
  destination: string;
  purpose: string;
  passengers: number;
  needs_driver: boolean;
  priority: string;
  priority_display: string;
  status: string;
  status_display: string;
  vehicle: string | null;
  vehicle_registration: string | null;
  driver: string | null;
  driver_name: string | null;
  requester_email: string | null;
  trip_id: string | null;
  validations: ReservationValidation[];
  created_at: string;
  updated_at: string;
}

export interface TripIncidentItem {
  id: string;
  occurred_at: string;
  severity: string;
  severity_display: string;
  description: string;
}

export interface Trip {
  id: string;
  reservation: string;
  requester: string;
  requester_name: string | null;
  subsidiary: string;
  subsidiary_name: string;
  vehicle: string;
  vehicle_registration: string;
  vehicle_label: string | null;
  driver: string | null;
  driver_name: string | null;
  destination: string;
  status: string;
  status_display: string;
  actual_departure: string | null;
  actual_return: string | null;
  start_mileage: number | null;
  end_mileage: number | null;
  distance_km: string | null;
  fuel_consumed: string | null;
  observations: string;
  estimated_fuel_l: number | null;
  fuel_intel: TripFuelIntel | null;
  incidents: TripIncidentItem[];
  created_at: string;
  updated_at: string;
}
