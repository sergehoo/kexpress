"use client";

import { useState } from "react";

import { Button, Label, Select, Spinner } from "@/components/ui";
import { Modal } from "@/components/Modal";
import {
  useDrivers,
  useReservationAction,
  useTripAction,
  useTripSuggestVehicle,
  useVehicles,
} from "@/lib/queries";
import { apiError } from "@/lib/api";
import type { Reservation, ReservationTripLeg } from "@/lib/types";

/** Modales du workflow de réservation, partagées entre les pages. */

/** Suffixe de titre indiquant le segment ciblé (aller / retour) pour une affectation
 *  PAR COURSE ; vide pour une affectation réservation-globale (raccourci « les deux »). */
function legSuffix(res: Reservation, trip?: ReservationTripLeg): string {
  if (!trip) return "";
  if (res.trip_type !== "round_trip") return " — course";
  return trip.leg === "outbound" ? " — aller" : " — retour";
}

export function RejectModal({
  res,
  onClose,
  onError,
}: {
  res: Reservation;
  onClose: () => void;
  onError: (s: string) => void;
}) {
  const reject = useReservationAction("reject");
  const [comment, setComment] = useState("");
  return (
    <Modal open title="Refuser la demande" onClose={onClose}>
      <Label>Motif du refus</Label>
      <textarea
        value={comment}
        onChange={(e) => setComment(e.target.value)}
        rows={3}
        className="w-full rounded-lg border border-line bg-surface p-3 text-sm text-ink outline-none focus:border-brand-400 focus:ring-2 focus:ring-brand-500/20"
      />
      <div className="flex justify-end gap-2 pt-3">
        <Button variant="secondary" onClick={onClose}>Annuler</Button>
        <Button
          variant="danger"
          disabled={reject.isPending}
          onClick={() =>
            reject.mutate(
              { id: res.id, body: { comment } },
              { onSuccess: onClose, onError: (e) => onError(apiError(e)) },
            )
          }
        >
          Confirmer le refus
        </Button>
      </div>
    </Modal>
  );
}

export function AssignVehicleModal({
  res,
  trip,
  onClose,
  onError,
}: {
  res: Reservation;
  /** Cible une COURSE précise (aller / retour) ; absent → affectation réservation-globale. */
  trip?: ReservationTripLeg;
  onClose: () => void;
  onError: (s: string) => void;
}) {
  const assignRes = useReservationAction("assign-vehicle");
  const assignTrip = useTripAction("assign-vehicle");
  const { data, isLoading } = useVehicles({ status: "available" });
  // Suggestions de proximité (dispatching) seulement en affectation par segment.
  const { data: suggestions } = useTripSuggestVehicle(trip?.id, !!trip);
  const [vehicle, setVehicle] = useState(trip?.vehicle ?? "");
  const vehicles = (data?.results ?? []).filter((v) => v.capacity >= res.passengers);
  const pending = trip ? assignTrip.isPending : assignRes.isPending;

  const submit = () => {
    const opts = { onSuccess: onClose, onError: (e: unknown) => onError(apiError(e)) };
    if (trip) assignTrip.mutate({ id: trip.id, body: { vehicle } }, opts);
    else assignRes.mutate({ id: res.id, body: { vehicle } }, opts);
  };

  return (
    <Modal open title={`Affecter un véhicule${legSuffix(res, trip)}`} onClose={onClose}>
      {isLoading ? (
        <Spinner />
      ) : (
        <>
          {suggestions && suggestions.length > 0 && (
            <div className="mb-3">
              <Label>Suggestions (au plus proche)</Label>
              <div className="flex flex-wrap gap-1.5">
                {suggestions.map((s) => (
                  <button
                    key={s.id}
                    type="button"
                    onClick={() => setVehicle(s.id)}
                    className={
                      "rounded-full border px-3 py-1 text-xs font-medium transition-colors " +
                      (vehicle === s.id
                        ? "border-brand-500 bg-brand-500/10 text-brand-700"
                        : "border-line text-muted hover:bg-surface2")
                    }
                  >
                    {s.registration}
                    {s.eta_min != null ? ` · ${s.eta_min} min` : ""}
                  </button>
                ))}
              </div>
            </div>
          )}
          <Label>Véhicule disponible (capacité ≥ {res.passengers})</Label>
          <Select value={vehicle} onChange={(e) => setVehicle(e.target.value)}>
            <option value="">— Choisir —</option>
            {vehicles.map((v) => (
              <option key={v.id} value={v.id} disabled={v.compliance ? !v.compliance.compliant : false}>
                {v.registration} · {v.brand} {v.model} ({v.capacity} pl.)
                {v.compliance && !v.compliance.compliant ? " — ⚠ non conforme" : ""}
              </option>
            ))}
          </Select>
          {vehicles.length === 0 && (
            <p className="mt-2 text-xs text-amber-600">
              Aucun véhicule disponible avec une capacité suffisante.
            </p>
          )}
          <div className="flex justify-end gap-2 pt-3">
            <Button variant="secondary" onClick={onClose}>Annuler</Button>
            <Button disabled={!vehicle || pending} onClick={submit}>Affecter</Button>
          </div>
        </>
      )}
    </Modal>
  );
}

export function AssignDriverModal({
  res,
  trip,
  onClose,
  onError,
}: {
  res: Reservation;
  /** Cible une COURSE précise (aller / retour) ; absent → affectation réservation-globale. */
  trip?: ReservationTripLeg;
  onClose: () => void;
  onError: (s: string) => void;
}) {
  const assignRes = useReservationAction("assign-driver");
  const assignTrip = useTripAction("assign-driver");
  const { data, isLoading } = useDrivers({ is_available: "true" });
  const [driver, setDriver] = useState(trip?.driver ?? "");
  const drivers = data?.results ?? [];
  const pending = trip ? assignTrip.isPending : assignRes.isPending;

  const submit = () => {
    const opts = { onSuccess: onClose, onError: (e: unknown) => onError(apiError(e)) };
    if (trip) assignTrip.mutate({ id: trip.id, body: { driver } }, opts);
    else assignRes.mutate({ id: res.id, body: { driver } }, opts);
  };

  return (
    <Modal open title={`Affecter un chauffeur${legSuffix(res, trip)}`} onClose={onClose}>
      {isLoading ? (
        <Spinner />
      ) : (
        <>
          <Label>Chauffeur disponible</Label>
          <Select value={driver} onChange={(e) => setDriver(e.target.value)}>
            <option value="">— Choisir —</option>
            {drivers.map((d) => (
              <option key={d.id} value={d.id}>
                {d.full_name} {d.license_category ? `· ${d.license_category}` : ""}
              </option>
            ))}
          </Select>
          <div className="flex justify-end gap-2 pt-3">
            <Button variant="secondary" onClick={onClose}>Annuler</Button>
            <Button disabled={!driver || pending} onClick={submit}>Affecter</Button>
          </div>
        </>
      )}
    </Modal>
  );
}
