"use client";

import { useState } from "react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import {
  AlertTriangle,
  ArrowLeft,
  Building2,
  Car,
  ClipboardList,
  Clock,
  Fuel,
  Gauge,
  MapPin,
  UserRound,
} from "lucide-react";

import { Button, Card, CardBody, EmptyState, Spinner } from "@/components/ui";
import { StatusBadge } from "@/components/StatusBadge";
import { EndTripModal, StartTripModal } from "@/components/trip-modals";
import { useGpsTracker } from "@/lib/useGpsTracker";
import { useTrip, useTripAction, useTripRoute } from "@/lib/queries";
import { apiError } from "@/lib/api";
import { cn, formatDate, formatNumber } from "@/lib/utils";

const MapView = dynamic(() => import("@/components/MapView"), {
  ssr: false,
  loading: () => <div className="flex h-full items-center justify-center"><Spinner className="h-6 w-6" /></div>,
});

type ModalState = "start" | "end" | null;

export default function TripDetailPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { data: t, isLoading, isError } = useTrip(id);
  const route = useTripRoute(id);
  const [modal, setModal] = useState<ModalState>(null);
  const [toast, setToast] = useState("");

  const close = useTripAction("close");
  // GPS réel : si la course est en cours, l'appareil du consultant participant
  // alimente le tracking (positions/vitesse réelles, aucune simulation serveur).
  const gps = useGpsTracker(t?.id, t?.status === "in_progress");

  if (isLoading) {
    return <div className="flex justify-center py-20"><Spinner className="h-7 w-7" /></div>;
  }
  if (isError || !t) {
    return (
      <Card>
        <CardBody>
          <EmptyState title="Course introuvable" hint="Elle a peut-être été supprimée ou vous n'y avez pas accès." />
          <div className="flex justify-center pt-2">
            <Button variant="secondary" onClick={() => router.push("/trips")}>
              <ArrowLeft className="h-4 w-4" /> Retour aux courses
            </Button>
          </div>
        </CardBody>
      </Card>
    );
  }

  const r = route.data;
  const fitTo = r?.planned?.length ? r.planned : undefined;

  return (
    <div className="mx-auto max-w-6xl space-y-4">
      {/* En-tête */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <Link href="/trips" className="inline-flex items-center gap-1 text-xs font-medium text-muted hover:text-ink">
            <ArrowLeft className="h-3.5 w-3.5" /> Courses
          </Link>
          <h1 className="mt-1 truncate text-xl font-bold text-ink">{t.destination}</h1>
          <p className="text-sm text-muted">
            {t.vehicle_label ? `${t.vehicle_label} · ` : ""}{t.vehicle_registration}
            {t.driver_name ? ` · ${t.driver_name}` : ""}
          </p>
        </div>
        <StatusBadge code={t.status} label={t.status_display} />
      </div>

      {toast && (
        <div className="flex items-center justify-between rounded-lg bg-rose-500/10 px-4 py-2 text-sm text-rose-600">
          {toast}
          <button onClick={() => setToast("")} className="text-rose-400 hover:text-rose-600">✕</button>
        </div>
      )}

      {/* Actions */}
      <Card>
        <CardBody className="flex flex-wrap items-center gap-2 py-3">
          {t.status === "scheduled" && (
            <Button size="sm" onClick={() => setModal("start")}>Démarrer la course</Button>
          )}
          {t.status === "in_progress" && (
            <Button size="sm" variant="success" onClick={() => setModal("end")}>Terminer la course</Button>
          )}
          {t.status === "returned" && (
            <Button
              size="sm"
              variant="secondary"
              disabled={close.isPending}
              onClick={() => close.mutate({ id: t.id }, { onError: (e) => setToast(apiError(e)) })}
            >
              Clôturer
            </Button>
          )}
          {t.status === "in_progress" && (
            <Link href="/map">
              <Button size="sm" variant="secondary"><MapPin className="h-4 w-4" /> Suivre en temps réel</Button>
            </Link>
          )}
          <Link href={`/reservations/${t.reservation}`} className="ml-auto">
            <Button size="sm" variant="ghost"><ClipboardList className="h-4 w-4" /> Voir la commande</Button>
          </Link>
        </CardBody>
      </Card>

      <div className="grid gap-4 lg:grid-cols-5">
        {/* Carte de l'itinéraire */}
        <Card className="lg:col-span-3">
          <div className="relative h-[22rem] overflow-hidden rounded-xl">
            <MapView
              positions={[]}
              planned={r?.planned}
              actual={r?.actual}
              destination={r?.destination_point}
              fitTo={fitTo}
            />
          </div>
          {r && (
            <CardBody className="space-y-1 py-2.5">
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
                <span>Itinéraire : <span className="font-semibold text-ink">{formatNumber(r.distance_km, "km")}</span></span>
                <span>Parcouru (GPS) : <span className="font-semibold text-ink">{formatNumber(r.traveled_km, "km")}</span></span>
                <span>Restant : <span className="font-semibold text-ink">{formatNumber(r.remaining_km, "km")}</span></span>
                <span>Progression : <span className="font-semibold text-ink">{Math.round((r.progress ?? 0) * 100)}%</span></span>
                <span>
                  Vitesse :{" "}
                  <span className="font-semibold text-ink">
                    {r.speed_kmh != null ? formatNumber(r.speed_kmh, "km/h") : "—"}
                  </span>
                </span>
              </div>
              {t.status === "in_progress" && (
                <p className={cn("text-[11px]", gps.active ? "text-emerald-600" : "text-faint")}>
                  {gps.active
                    ? "📡 GPS de l'appareil actif — positions réelles transmises"
                    : gps.error
                      ? `GPS indisponible : ${gps.error}`
                      : "En attente du GPS de l'appareil…"}
                </p>
              )}
            </CardBody>
          )}
        </Card>

        {/* Exécution + participants */}
        <div className="space-y-4 lg:col-span-2">
          <Card>
            <CardBody className="space-y-3">
              <SectionTitle>Exécution</SectionTitle>
              <div className="grid grid-cols-2 gap-3">
                <InfoRow icon={Clock} label="Départ réel" value={formatDate(t.actual_departure, true)} />
                <InfoRow icon={Clock} label="Retour réel" value={formatDate(t.actual_return, true)} />
                <InfoRow icon={Gauge} label="Km au départ" value={t.start_mileage != null ? formatNumber(t.start_mileage, "km") : "—"} />
                <InfoRow icon={Gauge} label="Km au retour" value={t.end_mileage != null ? formatNumber(t.end_mileage, "km") : "—"} />
                <InfoRow icon={Gauge} label="Distance parcourue" value={t.distance_km ? formatNumber(t.distance_km, "km") : "—"} />
                <InfoRow icon={Fuel} label="Carburant réel" value={t.fuel_consumed ? `${t.fuel_consumed} L` : "—"} />
              </div>
              {t.observations && <InfoRow label="Observations" value={t.observations} />}
            </CardBody>
          </Card>

          <Card>
            <CardBody className="space-y-3">
              <SectionTitle>Participants</SectionTitle>
              <InfoRow icon={UserRound} label="Demandeur" value={t.requester_name || "—"} />
              <InfoRow icon={UserRound} label="Chauffeur" value={t.driver_name || "Conduite personnelle"} />
              <InfoRow icon={Car} label="Véhicule" value={`${t.vehicle_label ?? ""} ${t.vehicle_registration}`.trim()} />
              <InfoRow icon={Building2} label="Filiale" value={t.subsidiary_name} />
            </CardBody>
          </Card>
        </div>
      </div>

      {/* Carburant & énergie */}
      <Card>
        <CardBody className="space-y-3">
          <SectionTitle>Carburant &amp; énergie</SectionTitle>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Kpi label="Consommation estimée" value={t.estimated_fuel_l != null ? `${t.estimated_fuel_l} L` : "—"} />
            <Kpi label="Consommation réelle" value={t.fuel_consumed ? `${t.fuel_consumed} L` : "—"} />
            {t.fuel_intel?.gap_pct != null && (
              <Kpi
                label="Écart estimé / réel"
                value={`${t.fuel_intel.gap_pct > 0 ? "+" : ""}${t.fuel_intel.gap_pct}%`}
                tone={t.fuel_intel.gap_pct > 15 ? "bad" : t.fuel_intel.gap_pct < -5 ? "good" : undefined}
              />
            )}
            {t.fuel_intel?.efficiency_score != null && (
              <Kpi
                label="Score énergie"
                value={`${t.fuel_intel.efficiency_score}/100`}
                tone={t.fuel_intel.efficiency_score >= 85 ? "good" : t.fuel_intel.efficiency_score < 60 ? "bad" : undefined}
              />
            )}
          </div>
          {/* Coûts — gestionnaires uniquement (fuel_intel est null pour l'employé) */}
          {t.fuel_intel?.fuel_cost != null && (
            <p className="rounded-lg bg-surface2 px-3 py-2 text-sm text-muted">
              Coût carburant : <span className="font-semibold text-ink">{formatNumber(t.fuel_intel.fuel_cost)} XOF</span>
              {t.fuel_intel.fuel_price != null && (
                <span className="text-faint"> — prix appliqué {formatNumber(t.fuel_intel.fuel_price)} XOF/L au {formatDate(t.fuel_intel.fuel_price_date)}</span>
              )}
            </p>
          )}
        </CardBody>
      </Card>

      {/* Incidents */}
      <Card>
        <CardBody className="space-y-3">
          <SectionTitle>Incidents</SectionTitle>
          {(t.incidents ?? []).length === 0 ? (
            <p className="text-sm text-faint">Aucun incident signalé sur cette course.</p>
          ) : (
            <ul className="space-y-2.5">
              {t.incidents.map((i) => (
                <li key={i.id} className="flex items-start gap-3">
                  <AlertTriangle
                    className={cn(
                      "mt-0.5 h-4 w-4 shrink-0",
                      i.severity === "major" || i.severity === "critical" ? "text-rose-500" : "text-amber-500",
                    )}
                  />
                  <div className="min-w-0">
                    <p className="text-sm font-medium text-ink">
                      {i.severity_display} · {formatDate(i.occurred_at, true)}
                    </p>
                    <p className="text-xs text-muted">{i.description}</p>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </CardBody>
      </Card>

      <p className="text-[11px] text-faint">
        Course {t.id} · créée le {formatDate(t.created_at, true)} · dernière mise à jour le {formatDate(t.updated_at, true)}
      </p>

      {modal === "start" && <StartTripModal trip={t} onClose={() => setModal(null)} onError={setToast} />}
      {modal === "end" && <EndTripModal trip={t} onClose={() => setModal(null)} onError={setToast} />}
    </div>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return <p className="text-xs font-semibold uppercase tracking-wide text-muted">{children}</p>;
}

function Kpi({ label, value, tone }: { label: string; value: string; tone?: "good" | "bad" }) {
  return (
    <div className="rounded-xl border border-line bg-surface2/60 px-3 py-2.5">
      <p className="text-[11px] uppercase tracking-wide text-faint">{label}</p>
      <p className={cn("text-lg font-bold text-ink", tone === "good" && "text-emerald-600", tone === "bad" && "text-rose-600")}>
        {value}
      </p>
    </div>
  );
}

function InfoRow({
  icon: Icon,
  label,
  value,
}: {
  icon?: React.ComponentType<{ className?: string }>;
  label: string;
  value: string;
}) {
  return (
    <div className="flex items-start gap-2.5">
      {Icon && <Icon className="mt-0.5 h-4 w-4 shrink-0 text-faint" />}
      <div className="min-w-0">
        <p className="text-[11px] uppercase tracking-wide text-faint">{label}</p>
        <p className="text-sm font-medium text-ink">{value}</p>
      </div>
    </div>
  );
}
