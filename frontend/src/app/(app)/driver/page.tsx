"use client";

import Link from "next/link";
import { Building2, Car, Clock, Flag, MapPin, Navigation, Play, User } from "lucide-react";

import { Card, CardBody, EmptyState, Spinner } from "@/components/ui";
import { useDriverMissions } from "@/lib/queries";
import { driverStatus } from "@/lib/driver";
import { useAuth } from "@/lib/auth";
import type { DriverMission } from "@/lib/types";
import { cn } from "@/lib/utils";

function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("fr-FR", { weekday: "short", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

const STATUS_TONE: Record<string, string> = {
  scheduled: "bg-violet-500/10 text-violet-600",
  departed: "bg-sky-500/10 text-sky-600",
  in_progress: "bg-sky-500/10 text-sky-600",
  returned: "bg-amber-500/10 text-amber-600",
};

export default function DriverDashboardPage() {
  const { me } = useAuth();
  const { data: missions, isLoading } = useDriverMissions(true);
  const status = driverStatus(missions);

  if (isLoading) return <div className="flex justify-center py-20"><Spinner className="h-8 w-8" /></div>;

  const list = missions ?? [];
  const active = list.filter((m) => ["in_progress", "departed", "returned"].includes(m.status));
  const upcoming = list.filter((m) => m.status === "scheduled");

  return (
    <div className="space-y-5">
      {/* En-tête chauffeur */}
      <Card>
        <CardBody className="flex flex-wrap items-center gap-4">
          <span className="flex h-14 w-14 items-center justify-center rounded-2xl bg-brand-500/10 text-brand-600"><Navigation className="h-7 w-7" /></span>
          <div className="min-w-0">
            <h1 className="text-lg font-semibold text-ink">Mon espace chauffeur</h1>
            <p className="text-sm text-muted">{me?.full_name || me?.email}</p>
          </div>
          <span className={cn("ml-auto rounded-full px-3 py-1 text-xs font-semibold ring-1 ring-inset", status.tone)}>
            {status.label}
          </span>
        </CardBody>
      </Card>

      {/* Course en cours — accès rapide à la carte */}
      {active.length > 0 && (
        <Link href="/map" className="block">
          <Card className="border-brand-500/30 bg-gradient-to-br from-brand-500/10 to-transparent transition-shadow hover:shadow-md">
            <CardBody className="flex items-center gap-4">
              <span className="flex h-11 w-11 items-center justify-center rounded-xl bg-brand-600 text-white"><Navigation className="h-5 w-5" /></span>
              <div className="min-w-0">
                <p className="font-semibold text-ink">Course {active[0].status === "returned" ? "à clôturer" : "en cours"}</p>
                <p className="truncate text-sm text-muted">→ {active[0].destination}</p>
              </div>
              <span className="ml-auto text-sm font-medium text-brand-600">Ouvrir la carte →</span>
            </CardBody>
          </Card>
        </Link>
      )}

      {/* À démarrer */}
      <section className="space-y-2">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-faint">À démarrer ({upcoming.length})</h2>
        {upcoming.length === 0 ? (
          <Card><CardBody><EmptyState title="Aucune mission à venir" /></CardBody></Card>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2">
            {upcoming.map((m) => <MissionCard key={m.trip_id} m={m} />)}
          </div>
        )}
      </section>

      {active.length === 0 && upcoming.length === 0 && (
        <Card><CardBody>
          <EmptyState title="Aucune mission assignée" hint="Vous serez notifié dès qu'une course vous sera affectée." />
        </CardBody></Card>
      )}
    </div>
  );
}

function MissionCard({ m }: { m: DriverMission }) {
  const res = m.reservation;
  return (
    <Link href="/map" className="block">
      <Card className="transition-shadow hover:shadow-md">
        <CardBody className="space-y-2.5">
          <div className="flex items-center justify-between">
            <span className="flex items-center gap-1.5">
              <span className={cn("rounded-full px-2 py-0.5 text-[11px] font-medium", STATUS_TONE[m.status] ?? "bg-surface2 text-muted")}>
                {m.status_display}
              </span>
              {m.trip_type === "round_trip" && (
                <span className="rounded-full bg-brand-500/10 px-2 py-0.5 text-[11px] font-medium text-brand-600">
                  {m.leg_display}
                </span>
              )}
            </span>
            {m.can_start && (
              <span className="inline-flex items-center gap-1 text-[11px] font-medium text-emerald-600"><Play className="h-3 w-3" /> Prête</span>
            )}
          </div>
          <div className="space-y-1 text-sm">
            <p className="flex items-start gap-2"><MapPin className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600" /><span className="truncate text-muted">{res?.origin || "Départ"}</span></p>
            <p className="flex items-start gap-2"><Flag className="mt-0.5 h-4 w-4 shrink-0 text-brand-600" /><span className="truncate font-medium text-ink">{m.destination}</span></p>
          </div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-line pt-2 text-[11px] text-muted">
            <span className="flex items-center gap-1"><Clock className="h-3 w-3" /> {fmtTime(res?.departure_time ?? null)}</span>
            {m.vehicle?.registration && <span className="flex items-center gap-1"><Car className="h-3 w-3" /> {m.vehicle.registration}</span>}
            {res?.requester_name && <span className="flex items-center gap-1"><User className="h-3 w-3" /> {res.requester_name}</span>}
            {m.subsidiary_name && <span className="flex items-center gap-1"><Building2 className="h-3 w-3" /> {m.subsidiary_name}</span>}
          </div>
        </CardBody>
      </Card>
    </Link>
  );
}
