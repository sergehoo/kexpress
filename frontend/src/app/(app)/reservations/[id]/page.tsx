"use client";

import { useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import {
  ArrowLeft,
  Building2,
  Car,
  CheckCircle2,
  Circle,
  Clock,
  Mail,
  MapPin,
  Route,
  UserRound,
  Users,
  XCircle,
} from "lucide-react";

import { Button, Card, CardBody, EmptyState, Spinner } from "@/components/ui";
import { StatusBadge } from "@/components/StatusBadge";
import {
  AssignDriverModal,
  AssignVehicleModal,
  RejectModal,
} from "@/components/reservation-modals";
import { useReservation, useReservationAction } from "@/lib/queries";
import { apiError } from "@/lib/api";
import type { Reservation } from "@/lib/types";
import { cn, formatDate } from "@/lib/utils";

type ModalState =
  | { type: "reject"; res: Reservation }
  | { type: "assign-vehicle"; res: Reservation }
  | { type: "assign-driver"; res: Reservation }
  | null;

export default function ReservationDetailPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { data: r, isLoading, isError } = useReservation(id);
  const [modal, setModal] = useState<ModalState>(null);
  const [toast, setToast] = useState("");

  const submit = useReservationAction("submit");
  const approve = useReservationAction("approve");
  const cancel = useReservationAction("cancel");
  const run = (m: ReturnType<typeof useReservationAction>) =>
    r && m.mutate({ id: r.id }, { onError: (e) => setToast(apiError(e)) });

  if (isLoading) {
    return <div className="flex justify-center py-20"><Spinner className="h-7 w-7" /></div>;
  }
  if (isError || !r) {
    return (
      <Card>
        <CardBody>
          <EmptyState title="Commande introuvable" hint="Elle a peut-être été supprimée ou vous n'y avez pas accès." />
          <div className="flex justify-center pt-2">
            <Button variant="secondary" onClick={() => router.push("/reservations")}>
              <ArrowLeft className="h-4 w-4" /> Retour aux réservations
            </Button>
          </div>
        </CardBody>
      </Card>
    );
  }

  const cancellable = [
    "draft", "submitted", "pending_manager", "pending_fleet",
    "approved", "vehicle_assigned", "driver_assigned",
  ].includes(r.status);

  return (
    <div className="mx-auto max-w-5xl space-y-4">
      {/* En-tête */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <Link href="/reservations" className="inline-flex items-center gap-1 text-xs font-medium text-muted hover:text-ink">
            <ArrowLeft className="h-3.5 w-3.5" /> Réservations
          </Link>
          <h1 className="mt-1 truncate text-xl font-bold text-ink">{r.destination}</h1>
          <p className="text-sm text-muted">{r.purpose}</p>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1.5">
          <StatusBadge code={r.status} label={r.status_display} />
          {["high", "urgent"].includes(r.priority) && (
            <StatusBadge code={r.priority} label={r.priority_display} />
          )}
        </div>
      </div>

      {toast && (
        <div className="flex items-center justify-between rounded-lg bg-rose-500/10 px-4 py-2 text-sm text-rose-600">
          {toast}
          <button onClick={() => setToast("")} className="text-rose-400 hover:text-rose-600">✕</button>
        </div>
      )}

      {/* Actions du workflow */}
      <Card>
        <CardBody className="flex flex-wrap items-center gap-2 py-3">
          {r.status === "draft" && (
            <Button size="sm" onClick={() => run(submit)} disabled={submit.isPending}>Soumettre</Button>
          )}
          {["pending_manager", "pending_fleet"].includes(r.status) && (
            <>
              <Button size="sm" variant="success" onClick={() => run(approve)} disabled={approve.isPending}>Valider</Button>
              <Button size="sm" variant="danger" onClick={() => setModal({ type: "reject", res: r })}>Refuser</Button>
            </>
          )}
          {r.status === "approved" && (
            <Button size="sm" onClick={() => setModal({ type: "assign-vehicle", res: r })}>Affecter véhicule</Button>
          )}
          {r.status === "vehicle_assigned" && r.needs_driver && (
            <Button size="sm" onClick={() => setModal({ type: "assign-driver", res: r })}>Affecter chauffeur</Button>
          )}
          {cancellable && (
            <Button size="sm" variant="ghost" onClick={() => run(cancel)} disabled={cancel.isPending}>Annuler la demande</Button>
          )}
          {r.trip_id && (
            <Link href={`/trips/${r.trip_id}`} className="ml-auto">
              <Button size="sm" variant="secondary"><Route className="h-4 w-4" /> Voir la course</Button>
            </Link>
          )}
          {!cancellable && !["pending_manager", "pending_fleet", "approved"].includes(r.status) && !r.trip_id && (
            <p className="text-xs text-faint">Aucune action disponible à ce statut.</p>
          )}
        </CardBody>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        {/* Trajet */}
        <Card>
          <CardBody className="space-y-3">
            <div className="flex items-center justify-between">
              <SectionTitle>Trajet demandé</SectionTitle>
              <span className="rounded-full bg-brand-500/10 px-2.5 py-0.5 text-[11px] font-medium text-brand-600">
                {r.trip_type_display}{r.trip_type === "round_trip" ? ` · ${r.voyages} voyages` : ""}
              </span>
            </div>
            <InfoRow icon={MapPin} label="Point de départ" value={r.origin || "—"} />
            <InfoRow icon={MapPin} label="Destination" value={r.destination} />
            <InfoRow icon={Clock} label="Date de la course" value={formatDate(r.trip_date)} />
            <div className="grid grid-cols-2 gap-3">
              <InfoRow icon={Clock} label="Départ (aller)" value={formatDate(r.departure_time, true)} />
              {r.trip_type === "round_trip" && r.return_time ? (
                <InfoRow icon={Clock} label="Départ (retour)" value={formatDate(r.return_time, true)} />
              ) : (
                <InfoRow icon={Clock} label="Retour estimé" value={formatDate(r.estimated_return, true)} />
              )}
            </div>
            {r.trip_type === "round_trip" && (
              <InfoRow icon={Clock} label="Fin de mission (estimée)" value={formatDate(r.estimated_return, true)} />
            )}
            <div className="grid grid-cols-2 gap-3">
              <InfoRow icon={Users} label="Passagers" value={String(r.passengers)} />
              <InfoRow icon={UserRound} label="Conduite" value={r.needs_driver ? "Avec chauffeur" : "Conduite personnelle"} />
            </div>
            <InfoRow label="Motif" value={r.purpose} />
            {r.trips.length > 1 && (
              <div className="space-y-1.5 border-t border-line pt-2">
                <p className="text-[11px] font-semibold uppercase tracking-wide text-faint">Courses ({r.trips.length})</p>
                {r.trips.map((t) => (
                  <div key={t.id} className="flex items-center justify-between text-xs">
                    <span className="text-muted">{t.leg_display} → <span className="text-ink">{t.destination}</span></span>
                    <span className="rounded-full bg-surface2 px-2 py-0.5 text-[10px] font-medium text-muted">{t.status_display}</span>
                  </div>
                ))}
              </div>
            )}
          </CardBody>
        </Card>

        {/* Demandeur + affectations */}
        <div className="space-y-4">
          <Card>
            <CardBody className="space-y-3">
              <SectionTitle>Demandeur</SectionTitle>
              <InfoRow icon={UserRound} label="Employé" value={r.requester_name} />
              <InfoRow icon={Mail} label="Email" value={r.requester_email || "—"} />
              <InfoRow icon={Building2} label="Filiale" value={r.subsidiary_name} />
            </CardBody>
          </Card>
          <Card>
            <CardBody className="space-y-3">
              <SectionTitle>Affectations</SectionTitle>
              {r.vehicle_registration || r.driver_name ? (
                <div className="flex flex-wrap gap-2">
                  {r.vehicle_registration && (
                    <span className="inline-flex items-center gap-1.5 rounded-lg bg-sky-500/10 px-3 py-1.5 text-sm font-medium text-sky-600">
                      <Car className="h-4 w-4" /> {r.vehicle_registration}
                    </span>
                  )}
                  {r.driver_name && (
                    <span className="inline-flex items-center gap-1.5 rounded-lg bg-violet-500/10 px-3 py-1.5 text-sm font-medium text-violet-600">
                      <UserRound className="h-4 w-4" /> {r.driver_name}
                    </span>
                  )}
                </div>
              ) : (
                <p className="text-sm text-faint">Aucun véhicule ni chauffeur affecté pour le moment.</p>
              )}
            </CardBody>
          </Card>
        </div>
      </div>

      {/* Circuit de validation */}
      <Card>
        <CardBody className="space-y-3">
          <SectionTitle>Circuit de validation</SectionTitle>
          {r.validations.length === 0 ? (
            <p className="text-sm text-faint">Aucune étape de validation enregistrée (demande non soumise).</p>
          ) : (
            <ol className="space-y-3">
              {r.validations.map((v) => {
                const Icon = v.decision === "approved" ? CheckCircle2 : v.decision === "rejected" ? XCircle : Circle;
                const color =
                  v.decision === "approved" ? "text-emerald-500" : v.decision === "rejected" ? "text-rose-500" : "text-amber-400";
                return (
                  <li key={v.id} className="flex items-start gap-3">
                    <Icon className={cn("mt-0.5 h-5 w-5 shrink-0", color)} />
                    <div className="min-w-0">
                      <p className="text-sm font-medium text-ink">
                        {v.level_display} — {v.decision_display}
                      </p>
                      <p className="text-xs text-muted">
                        {v.validator_name || "Validateur non renseigné"}
                        {v.decided_at ? ` · ${formatDate(v.decided_at, true)}` : " · en attente"}
                      </p>
                      {v.comment && <p className="mt-1 rounded-md bg-surface2 px-2.5 py-1.5 text-xs text-muted">« {v.comment} »</p>}
                    </div>
                  </li>
                );
              })}
            </ol>
          )}
        </CardBody>
      </Card>

      <p className="text-[11px] text-faint">
        Commande {r.id} · créée le {formatDate(r.created_at, true)} · dernière mise à jour le {formatDate(r.updated_at, true)}
      </p>

      {modal?.type === "reject" && <RejectModal res={modal.res} onClose={() => setModal(null)} onError={setToast} />}
      {modal?.type === "assign-vehicle" && <AssignVehicleModal res={modal.res} onClose={() => setModal(null)} onError={setToast} />}
      {modal?.type === "assign-driver" && <AssignDriverModal res={modal.res} onClose={() => setModal(null)} onError={setToast} />}
    </div>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return <p className="text-xs font-semibold uppercase tracking-wide text-muted">{children}</p>;
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
