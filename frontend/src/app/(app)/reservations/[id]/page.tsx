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
  CornerUpLeft,
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
import { useReservation, useReservationAction, useTripAction } from "@/lib/queries";
import { useAuth } from "@/lib/auth";
import { canManageFleet } from "@/lib/rbac";
import { apiError } from "@/lib/api";
import type { Reservation, ReservationTripLeg } from "@/lib/types";
import { cn, formatDate } from "@/lib/utils";

type ModalState =
  | { type: "reject"; res: Reservation }
  | { type: "assign-vehicle"; res: Reservation; trip?: ReservationTripLeg }
  | { type: "assign-driver"; res: Reservation; trip?: ReservationTripLeg }
  | null;

/** Libellé de course lisible : « Aller » / « Retour » pour un A/R, « Course » sinon. */
function courseLabel(res: Reservation, t: ReservationTripLeg): string {
  return res.trip_type === "round_trip" ? t.leg_display : "Course";
}

export default function ReservationDetailPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { data: r, isLoading, isError } = useReservation(id);
  const { me } = useAuth();
  const [modal, setModal] = useState<ModalState>(null);
  const [toast, setToast] = useState("");

  const submit = useReservationAction("submit");
  const approve = useReservationAction("approve");
  const cancel = useReservationAction("cancel");
  const cancelTrip = useTripAction("cancel");
  const run = (m: ReturnType<typeof useReservationAction>) =>
    r && m.mutate({ id: r.id }, { onError: (e) => setToast(apiError(e)) });
  const canManage = canManageFleet(me?.role) || Boolean(me?.has_company_scope);

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
          {/* Repli legacy : réservations validées AVANT la gestion par course (aucune course
              générée) — l'affectation réservation-globale reste possible. Sinon, l'affectation
              se fait par course dans la section « Courses » ci-dessous. */}
          {r.trips.length === 0 && r.status === "approved" && (
            <Button size="sm" onClick={() => setModal({ type: "assign-vehicle", res: r })}>Affecter véhicule</Button>
          )}
          {r.trips.length === 0 && r.status === "vehicle_assigned" && r.needs_driver && (
            <Button size="sm" onClick={() => setModal({ type: "assign-driver", res: r })}>Affecter chauffeur</Button>
          )}
          {cancellable && (
            <Button size="sm" variant="ghost" onClick={() => run(cancel)} disabled={cancel.isPending}>Annuler la demande</Button>
          )}
          {!cancellable && !["pending_manager", "pending_fleet", "approved"].includes(r.status) && r.trips.length === 0 && (
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
          </CardBody>
        </Card>

        {/* Demandeur */}
        <div className="space-y-4">
          <Card>
            <CardBody className="space-y-3">
              <SectionTitle>Demandeur</SectionTitle>
              <InfoRow icon={UserRound} label="Employé" value={r.requester_name} />
              <InfoRow icon={Mail} label="Email" value={r.requester_email || "—"} />
              <InfoRow icon={Building2} label="Filiale" value={r.subsidiary_name} />
            </CardBody>
          </Card>
        </div>
      </div>

      {/* Courses (aller / retour) — affectation, horaires et statut INDÉPENDANTS par segment */}
      {r.trips.length > 0 && (
        <Card>
          <CardBody className="space-y-3">
            <SectionTitle>
              {r.trip_type === "round_trip" ? `Courses — aller-retour (${r.trips.length})` : "Course"}
            </SectionTitle>
            <div className={cn("grid gap-3", r.trips.length > 1 && "sm:grid-cols-2")}>
              {r.trips.map((t) => (
                <CourseCard
                  key={t.id}
                  res={r}
                  trip={t}
                  canManage={canManage}
                  cancelPending={cancelTrip.isPending}
                  onAssignVehicle={() => setModal({ type: "assign-vehicle", res: r, trip: t })}
                  onAssignDriver={() => setModal({ type: "assign-driver", res: r, trip: t })}
                  onCancel={() =>
                    cancelTrip.mutate({ id: t.id }, { onError: (e) => setToast(apiError(e)) })
                  }
                />
              ))}
            </div>
          </CardBody>
        </Card>
      )}

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
      {modal?.type === "assign-vehicle" && <AssignVehicleModal res={modal.res} trip={modal.trip} onClose={() => setModal(null)} onError={setToast} />}
      {modal?.type === "assign-driver" && <AssignDriverModal res={modal.res} trip={modal.trip} onClose={() => setModal(null)} onError={setToast} />}
    </div>
  );
}

/** Carte d'une course (segment) : itinéraire, horaires prévus, affectation et statut PROPRES,
 *  avec actions indépendantes (affecter véhicule/chauffeur, annuler) — cœur de la gestion A/R. */
function CourseCard({
  res,
  trip,
  canManage,
  cancelPending,
  onAssignVehicle,
  onAssignDriver,
  onCancel,
}: {
  res: Reservation;
  trip: ReservationTripLeg;
  canManage: boolean;
  cancelPending: boolean;
  onAssignVehicle: () => void;
  onAssignDriver: () => void;
  onCancel: () => void;
}) {
  // Une course n'est (ré)affectable / annulable que tant qu'elle n'a pas démarré.
  const assignable = trip.status === "scheduled";
  const isReturn = trip.leg === "return";
  return (
    <div className="rounded-xl border border-line bg-surface2/40 p-3.5">
      <div className="flex items-center justify-between gap-2">
        <span className="inline-flex items-center gap-1.5 text-sm font-semibold text-ink">
          {isReturn ? <CornerUpLeft className="h-4 w-4 text-brand-500" /> : <Route className="h-4 w-4 text-brand-500" />}
          {courseLabel(res, trip)}
        </span>
        <StatusBadge code={trip.status} label={trip.status_display} />
      </div>

      <div className="mt-2.5 flex items-center gap-1.5 text-sm text-ink">
        <MapPin className="h-3.5 w-3.5 shrink-0 text-faint" />
        <span className="truncate">{trip.origin || "—"}</span>
        <span className="text-faint">→</span>
        <span className="truncate font-medium">{trip.destination}</span>
      </div>
      {(trip.planned_departure_at || trip.planned_arrival_at) && (
        <p className="mt-1 flex items-center gap-1.5 text-xs text-muted">
          <Clock className="h-3.5 w-3.5 shrink-0 text-faint" />
          {trip.planned_departure_at ? formatDate(trip.planned_departure_at, true) : "—"}
          {" → "}
          {trip.planned_arrival_at ? formatDate(trip.planned_arrival_at, true) : "—"}
        </p>
      )}

      <div className="mt-2.5 flex flex-wrap gap-1.5">
        <span className={cn(
          "inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-medium",
          trip.vehicle_registration ? "bg-sky-500/10 text-sky-600" : "bg-surface2 text-faint",
        )}>
          <Car className="h-3.5 w-3.5" /> {trip.vehicle_registration || "Véhicule non affecté"}
        </span>
        {res.needs_driver && (
          <span className={cn(
            "inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-medium",
            trip.driver_name ? "bg-violet-500/10 text-violet-600" : "bg-surface2 text-faint",
          )}>
            <UserRound className="h-3.5 w-3.5" /> {trip.driver_name || "Chauffeur non affecté"}
          </span>
        )}
      </div>

      {canManage && assignable && (
        <div className="mt-3 flex flex-wrap gap-1.5 border-t border-line pt-2.5">
          <Button size="sm" variant="secondary" onClick={onAssignVehicle}>
            {trip.vehicle ? "Changer véhicule" : "Affecter véhicule"}
          </Button>
          {res.needs_driver && (
            <Button size="sm" variant="secondary" onClick={onAssignDriver}>
              {trip.driver ? "Changer chauffeur" : "Affecter chauffeur"}
            </Button>
          )}
          <Button size="sm" variant="ghost" onClick={onCancel} disabled={cancelPending}>Annuler la course</Button>
        </div>
      )}
      <div className="mt-2">
        <Link href={`/trips/${trip.id}`} className="inline-flex items-center gap-1 text-xs font-medium text-brand-600 hover:underline">
          <Route className="h-3.5 w-3.5" /> Voir la course
        </Link>
      </div>
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
