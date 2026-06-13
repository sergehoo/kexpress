"use client";

import { useMemo, useState } from "react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { CalendarClock, CheckCircle2, Clock, UserRound, Users } from "lucide-react";

import { Button, Card, CardBody, Select, Spinner } from "@/components/ui";
import { Modal } from "@/components/Modal";
import { StatusBadge } from "@/components/StatusBadge";
import { StatChips } from "@/components/StatChips";
import { useDrivers, useReservations } from "@/lib/queries";
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
  approved: "#10b981",
  vehicle_assigned: "#8b5cf6",
  driver_assigned: "#8b5cf6",
  in_progress: "#3b82f6",
  completed: "#10b981",
  closed: "#64748b",
  pending_manager: "#f59e0b",
  pending_fleet: "#f59e0b",
};

export default function PlanningDriversPage() {
  const [driverId, setDriverId] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const { data: driversData } = useDrivers({ page_size: "100" });
  const drivers = driversData?.results ?? [];

  const params: Record<string, string> = { page_size: "200" };
  if (driverId) params.driver = driverId;
  const { data, isLoading } = useReservations(params);
  const assigned = useMemo(
    () => (data?.results ?? []).filter((r) => r.driver),
    [data],
  );

  const nameById = useMemo(
    () => Object.fromEntries(drivers.map((d) => [d.id, d.full_name])),
    [drivers],
  );

  const events = useMemo(
    () =>
      assigned.map((r) => ({
        title: `${nameById[r.driver as string] ?? "Chauffeur"} · ${r.destination}`,
        start: r.departure_time,
        end: r.estimated_return,
        backgroundColor: STATUS_COLOR[r.status] ?? "#64748b",
        borderColor: STATUS_COLOR[r.status] ?? "#64748b",
        extendedProps: { id: r.id },
      })),
    [assigned, nameById],
  );

  // Statistiques de charge chauffeurs
  const activeNow = assigned.filter((r) => r.status === "in_progress").length;
  const busyDrivers = new Set(assigned.map((r) => r.driver)).size;
  const available = drivers.filter((d) => d.is_available).length;
  const load = drivers.length ? Math.round((busyDrivers / drivers.length) * 100) : 0;

  const selected = assigned.find((r) => r.id === selectedId) ?? null;

  return (
    <div className="space-y-4">
      <StatChips
        stats={[
          { label: "Courses assignées", value: assigned.length, icon: CalendarClock, tone: "bg-brand-500/10 text-brand-600" },
          { label: "En cours", value: activeNow, icon: Clock, tone: "bg-sky-500/10 text-sky-600" },
          { label: "Chauffeurs planifiés", value: `${busyDrivers}/${drivers.length}`, icon: Users, tone: "bg-violet-500/10 text-violet-600" },
          { label: "Disponibles", value: available, icon: CheckCircle2, tone: "bg-emerald-500/10 text-emerald-600" },
          { label: "Taux de charge", value: `${load}%`, icon: UserRound, tone: "bg-amber-500/10 text-amber-600" },
        ]}
      />

      <div className="flex items-center gap-3">
        <Select value={driverId} onChange={(e) => setDriverId(e.target.value)} className="sm:w-64">
          <option value="">Tous les chauffeurs</option>
          {drivers.map((d) => (
            <option key={d.id} value={d.id}>{d.full_name}</option>
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

      {selected && (
        <Modal open title="Détail de l'affectation" onClose={() => setSelectedId(null)}>
          <div className="space-y-2 text-sm">
            <div className="flex items-start justify-between gap-2">
              <p className="font-semibold text-ink">{selected.destination}</p>
              <StatusBadge code={selected.status} label={selected.status_display} />
            </div>
            <div className="grid grid-cols-2 gap-2 rounded-lg bg-surface2 p-3 text-xs text-muted">
              <span>Chauffeur : <b className="text-ink">{selected.driver_name ?? "—"}</b></span>
              <span>Véhicule : <b className="text-ink">{selected.vehicle_registration ?? "—"}</b></span>
              <span>Départ : <b className="text-ink">{formatDate(selected.departure_time, true)}</b></span>
              <span>Retour : <b className="text-ink">{formatDate(selected.estimated_return, true)}</b></span>
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
