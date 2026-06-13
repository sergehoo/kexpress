"use client";

import { useState } from "react";

import { Button, Input, Label } from "@/components/ui";
import { Modal } from "@/components/Modal";
import { useTripAction } from "@/lib/queries";
import { apiError } from "@/lib/api";
import type { Trip } from "@/lib/types";

export function StartTripModal({ trip, onClose, onError }: { trip: Trip; onClose: () => void; onError: (s: string) => void }) {
  const start = useTripAction("start");
  const [km, setKm] = useState("");
  return (
    <Modal open title="Démarrer la course" onClose={onClose}>
      <Label>Kilométrage au départ (optionnel)</Label>
      <Input type="number" min={0} value={km} onChange={(e) => setKm(e.target.value)} placeholder="ex. 25000" />
      <div className="flex justify-end gap-2 pt-3">
        <Button variant="secondary" onClick={onClose}>Annuler</Button>
        <Button
          disabled={start.isPending}
          onClick={() =>
            start.mutate(
              { id: trip.id, body: km ? { start_mileage: Number(km) } : {} },
              { onSuccess: onClose, onError: (e) => onError(apiError(e)) },
            )
          }
        >
          Confirmer le départ
        </Button>
      </div>
    </Modal>
  );
}

export function EndTripModal({ trip, onClose, onError }: { trip: Trip; onClose: () => void; onError: (s: string) => void }) {
  const end = useTripAction("end");
  const [km, setKm] = useState("");
  const [fuel, setFuel] = useState("");
  return (
    <Modal open title="Terminer la course" onClose={onClose}>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <Label>Km au retour</Label>
          <Input type="number" min={0} value={km} onChange={(e) => setKm(e.target.value)} required />
        </div>
        <div>
          <Label>Carburant (L)</Label>
          <Input type="number" min={0} step="0.01" value={fuel} onChange={(e) => setFuel(e.target.value)} />
        </div>
      </div>
      <div className="flex justify-end gap-2 pt-3">
        <Button variant="secondary" onClick={onClose}>Annuler</Button>
        <Button
          variant="success"
          disabled={!km || end.isPending}
          onClick={() =>
            end.mutate(
              {
                id: trip.id,
                body: { end_mileage: Number(km), ...(fuel ? { fuel_consumed: Number(fuel) } : {}) },
              },
              { onSuccess: onClose, onError: (e) => onError(apiError(e)) },
            )
          }
        >
          Enregistrer le retour
        </Button>
      </div>
    </Modal>
  );
}
