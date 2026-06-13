"use client";

import { useState } from "react";

import { Button, Label, Select, Spinner } from "@/components/ui";
import { Modal } from "@/components/Modal";
import { useDrivers, useReservationAction, useVehicles } from "@/lib/queries";
import { apiError } from "@/lib/api";
import type { Reservation } from "@/lib/types";

/** Modales du workflow de réservation, partagées entre les pages. */

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
  onClose,
  onError,
}: {
  res: Reservation;
  onClose: () => void;
  onError: (s: string) => void;
}) {
  const assign = useReservationAction("assign-vehicle");
  const { data, isLoading } = useVehicles({ status: "available" });
  const [vehicle, setVehicle] = useState("");
  const vehicles = (data?.results ?? []).filter((v) => v.capacity >= res.passengers);
  return (
    <Modal open title="Affecter un véhicule" onClose={onClose}>
      {isLoading ? (
        <Spinner />
      ) : (
        <>
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
            <Button
              disabled={!vehicle || assign.isPending}
              onClick={() =>
                assign.mutate(
                  { id: res.id, body: { vehicle } },
                  { onSuccess: onClose, onError: (e) => onError(apiError(e)) },
                )
              }
            >
              Affecter
            </Button>
          </div>
        </>
      )}
    </Modal>
  );
}

export function AssignDriverModal({
  res,
  onClose,
  onError,
}: {
  res: Reservation;
  onClose: () => void;
  onError: (s: string) => void;
}) {
  const assign = useReservationAction("assign-driver");
  const { data, isLoading } = useDrivers({ is_available: "true" });
  const [driver, setDriver] = useState("");
  const drivers = data?.results ?? [];
  return (
    <Modal open title="Affecter un chauffeur" onClose={onClose}>
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
            <Button
              disabled={!driver || assign.isPending}
              onClick={() =>
                assign.mutate(
                  { id: res.id, body: { driver } },
                  { onSuccess: onClose, onError: (e) => onError(apiError(e)) },
                )
              }
            >
              Affecter
            </Button>
          </div>
        </>
      )}
    </Modal>
  );
}
