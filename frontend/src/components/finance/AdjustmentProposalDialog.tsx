"use client";

import { useState } from "react";

import { Button } from "@/components/ui";
import { Modal } from "@/components/Modal";
import { apiError } from "@/lib/api";
import { useCreateAdjustment, type AdjustmentProposal } from "@/lib/financeF2";
import { formatNumber } from "@/lib/utils";

/** Proposition d'ajustement financier : affichée quand l'API refuse d'écrire dans un coût
 *  figé ou un mois clos (409 + `adjustment_proposal`). Au lieu d'une impasse, l'utilisateur
 *  crée en un clic un ajustement comptabilisé sur la période ouverte, rattaché à la période
 *  d'origine, et qu'une autre personne approuvera. */
export function AdjustmentProposalDialog({ proposal, onClose, onCreated }: {
  proposal: AdjustmentProposal;
  onClose: () => void;
  onCreated?: () => void;
}) {
  const [reason, setReason] = useState("");
  const [amount, setAmount] = useState(proposal.amount ?? "");
  const [error, setError] = useState("");
  const create = useCreateAdjustment();

  function submit() {
    setError("");
    if (!reason.trim()) { setError("Le motif est obligatoire."); return; }
    create.mutate({
      original_period: proposal.original_period, amount: String(amount), reason,
      source: proposal.source || "other", source_id: proposal.source_id, subsidiary: proposal.subsidiary,
      trip: proposal.trip, mission: proposal.mission, vehicle: proposal.vehicle,
      category: proposal.category,
    }, { onSuccess: () => { onCreated?.(); onClose(); }, onError: (e) => setError(apiError(e)) });
  }

  return (
    <Modal open title="Créer un ajustement financier" onClose={onClose}>
      <div className="space-y-3 text-sm">
        <p className="rounded-lg border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-800">{proposal.detail}</p>
        <div className="grid grid-cols-2 gap-2 rounded-lg bg-surface2 p-3 text-xs text-muted">
          <span>Période d&apos;origine : <b className="text-ink">{proposal.original_period}</b></span>
          <span>Comptabilisé sur : <b className="text-ink">{proposal.posting_period}</b></span>
          {proposal.amount != null && <span>Montant proposé : <b className="text-ink">{formatNumber(proposal.amount)} XOF</b></span>}
          <span>Objet : <b className="text-ink">{proposal.source}</b></span>
        </div>
        <label className="block text-xs font-medium text-muted">Montant
          <input type="number" step="0.01" value={amount} onChange={(e) => setAmount(e.target.value)}
                 className="mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2 text-sm text-ink" />
        </label>
        <label className="block text-xs font-medium text-muted">Motif
          <textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={3}
                    className="mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2 text-sm text-ink"
                    placeholder="Ex. facture reçue après la clôture du mois" />
        </label>
        <p className="text-[11px] text-faint">L&apos;ajustement sera à approuver par une autre personne ; ses justificatifs se joignent ensuite depuis l&apos;onglet Ajustements.</p>
        {error && <p className="text-xs text-red-600">{error}</p>}
      </div>
      <div className="flex justify-end gap-2 pt-3">
        <Button variant="secondary" onClick={onClose}>Annuler</Button>
        <Button onClick={submit} disabled={create.isPending}>Créer l&apos;ajustement</Button>
      </div>
    </Modal>
  );
}
