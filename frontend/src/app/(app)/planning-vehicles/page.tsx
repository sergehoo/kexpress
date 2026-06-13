"use client";

import { useMemo, useState } from "react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { CalendarCheck, Car, CheckCircle2, Clock, Gauge } from "lucide-react";

import { Button, Card, CardBody, Select, Spinner } from "@/components/ui";
import { Modal } from "@/components/Modal";
import { StatusBadge } from "@/components/StatusBadge";
import { StatChips } from "@/components/StatChips";
import { useReservations, useVehicles } from "@/lib/queries";
import { formatDate } from "@/lib/utils";

const CalendarView = dynamic(() => import("@/components/CalendarView"), {
  ssr: false,
  loading: () => (
    <div className="flex h-[72vh] items-center justify-center">
      <Spinner className="h-7 w-7" />
    </div>
  ),
});

const STATUS_COLOR: Record<string, string> = {
  draft: "#94a3b8",
  submitted: "#0ea5e9",
  pending_manager: "#f59e0b",
  pending_fleet: "#f59e0b",
  approved: "#10b981",
  vehicle_assigned: "#8b5cf6",
  driver_assigned: "#8b5cf6",
  in_progress: "#3b82f6",
  completed: "#10b981",
  closed: "#64748b",
  rejected: "#f43f5e",
  cancelled: "#94a3b8",
};

export default function PlanningVehiclesPage() {
  const [vehicleId, setVehicleId] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const { data: vehiclesData } = useVehicles({ page_size: "100" });
  const vehicles = vehiclesData?.results ?? [];

  const params: Record<string, string> = { page_size: "200" };
  if (vehicleId) params.vehicle = vehicleId;
  const { data, isLoading } = useReservations(params);
  const reservations = useMemo(
    () => (data?.results ?? []).filter((r) => r.vehicle),
    [data],
  );

  const regById = useMemo(
    () => Object.fromEntries(vehicles.map((v) => [v.id, v.registration])),
    [vehicles],
  );

  const events = useMemo(
    () =>
      reservations.map((r) => ({
        title: `${regById[r.vehicle as string] ?? "Véhicule"} · ${r.destination}`,
        start: r.departure_time,
        end: r.estimated_return,
        backgroundColor: STATUS_COLOR[r.status] ?? "#64748b",
        borderColor: STATUS_COLOR[r.status] ?? "#64748b",
        extendedProps: { id: r.id },
      })),
    [reservations, regById],
  );

  // Statistiques du planning affiché
  const planned = reservations.filter((r) =>
    ["approved", "vehicle_assigned", "driver_assigned"].includes(r.status)).length;
  const active = reservations.filter((r) => r.status === "in_progress").length;
  const distinctVehicles = new Set(reservations.map((r) => r.vehicle)).size;
  const occupancy = vehicles.length ? Math.round((distinctVehicles / vehicles.length) * 100) : 0;

  const selected = reservations.find((r) => r.id === selectedId) ?? null;

  return (
    <div className="space-y-4">
      <StatChips
        stats={[
          { label: "Réservations planifiées", value: reservations.length, icon: CalendarCheck, tone: "bg-brand-500/10 text-brand-600" },
          { label: "Prêtes / affectées", value: planned, icon: CheckCircle2, tone: "bg-violet-500/10 text-violet-600" },
          { label: "En cours", value: active, icon: Clock, tone: "bg-sky-500/10 text-sky-600" },
          { label: "Véhicules planifiés", value: `${distinctVehicles}/${vehicles.length}`, icon: Car, tone: "bg-emerald-500/10 text-emerald-600" },
          { label: "Taux de planification", value: `${occupancy}%`, icon: Gauge, tone: "bg-amber-500/10 text-amber-600" },
        ]}
      />

      <div className="flex items-center gap-3">
        <Select value={vehicleId} onChange={(e) => setVehicleId(e.target.value)} className="sm:w-64">
          <option value="">Tous les véhicules</option>
          {vehicles.map((v) => (
            <option key={v.id} value={v.id}>{v.registration} — {v.brand} {v.model}</option>
          ))}
        </Select>
        <span className="ml-auto text-xs text-faint">Cliquez sur un créneau pour le détail.</span>
      </div>

      <Card>
        <CardBody>
          {isLoading ? (
            <div className="flex h-[72vh] items-center justify-center"><Spinner className="h-7 w-7" /></div>
          ) : (
            <CalendarView events={events} onEventClick={setSelectedId} />
          )}
        </CardBody>
      </Card>

      {/* Détail du créneau sélectionné */}
      {selected && (
        <Modal open title="Détail de la réservation" onClose={() => setSelectedId(null)}>
          <div className="space-y-2 text-sm">
            <div className="flex items-start justify-between gap-2">
              <p className="font-semibold text-ink">{selected.destination}</p>
              <StatusBadge code={selected.status} label={selected.status_display} />
            </div>
            <p className="text-muted">{selected.purpose}</p>
            <div className="grid grid-cols-2 gap-2 rounded-lg bg-surface2 p-3 text-xs text-muted">
              <span>Départ : <b className="text-ink">{formatDate(selected.departure_time, true)}</b></span>
              <span>Retour : <b className="text-ink">{formatDate(selected.estimated_return, true)}</b></span>
              <span>Véhicule : <b className="text-ink">{selected.vehicle_registration ?? "—"}</b></span>
              <span>Chauffeur : <b className="text-ink">{selected.driver_name ?? "—"}</b></span>
              <span>Demandeur : <b className="text-ink">{selected.requester_name}</b></span>
              <span>Passagers : <b className="text-ink">{selected.passengers}</b></span>
            </div>
          </div>
          <div className="flex justify-end gap-2 pt-3">
            <Button variant="secondary" onClick={() => setSelectedId(null)}>Fermer</Button>
            <Link href={`/reservations/${selected.id}`}>
              <Button>Fiche complète</Button>
            </Link>
          </div>
        </Modal>
      )}
    </div>
  );
}
