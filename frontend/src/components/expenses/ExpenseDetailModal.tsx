"use client";

import { useState } from "react";
import Link from "next/link";
import { ArrowRight, Pencil, Send, Trash2 } from "lucide-react";

import { Button, Input, Spinner } from "@/components/ui";
import { Modal } from "@/components/Modal";
import { apiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { canFinance } from "@/lib/rbac";
import {
  adjustmentProposal, PAYMENT_METHODS, useExpenseAction, useExpenseHistory,
  type AdjustmentProposal, type ExpenseAction, type WorkflowExpense,
} from "@/lib/financeF2";
import { formatDate, formatNumber } from "@/lib/utils";

import {
  EDITABLE, ExpenseStatusBadge, LateExpenseBanner, NonCountableBadge, Notice, ReceiptMissingBadge,
} from "./ExpenseBadges";
import { ExpenseAllocations } from "./ExpenseAllocations";
import { ExpenseAttachments } from "./ExpenseAttachments";
import { ExpenseHistory, lastRejection, pendingInfoRequest } from "./ExpenseHistory";
import { useDiscardDraft, useExpenseDetail } from "./useExpenseDetail";

const ADJUSTMENT_STATUS: Record<string, string> = {
  pending: "en attente d'approbation", approved: "approuvé", rejected: "rejeté",
};

function Item({ label, children, full }: { label: string; children: React.ReactNode; full?: boolean }) {
  return (
    <span className={full ? "col-span-2" : ""}>
      {label} : <b className="font-semibold text-ink">{children}</b>
    </span>
  );
}

/** Détail d'une dépense : imputation, justificatifs, historique, répartition de mission et
 *  gestes de l'AUTEUR (soumettre, transmettre, abandonner). Valider, payer et annuler relèvent
 *  de la file Finance : on y renvoie les profils habilités plutôt que de dupliquer l'écran. */
export function ExpenseDetailModal({ id, fallback, notice, onClose, onEdit, onProposal, onDiscarded }: {
  id: string;
  fallback?: WorkflowExpense;
  /** Message contextuel (ex. « brouillon enregistré »). */
  notice?: React.ReactNode;
  onClose: () => void;
  onEdit: (row: WorkflowExpense) => void;
  onProposal: (proposal: AdjustmentProposal) => void;
  onDiscarded: () => void;
}) {
  const { me } = useAuth();
  const { data: row, isLoading } = useExpenseDetail(id, fallback);
  const { data: history } = useExpenseHistory(id);
  const action = useExpenseAction();
  const discard = useDiscardDraft();
  const [comment, setComment] = useState("");
  const [error, setError] = useState("");
  const [result, setResult] = useState<{ tone: "success" | "warning"; text: string } | null>(null);
  const [confirmDiscard, setConfirmDiscard] = useState(false);

  if (!row) {
    return (
      <Modal open title="Détail de la dépense" onClose={onClose}>
        <div className="flex justify-center py-10">
          {isLoading ? <Spinner className="h-6 w-6" /> : <p className="text-sm text-muted">Dépense introuvable.</p>}
        </div>
      </Modal>
    );
  }

  const can = (codename: string) => canFinance(me, codename);
  // Dépense sans auteur connu (reprise) : les gestes restent possibles, l'API tranche.
  const isAuthor = !row.author || row.author === me?.id;
  const editable = EDITABLE.includes(row.status);
  const canEdit = can("create_expense") && editable;
  const canSubmit = isAuthor && can("submit_expense") && row.status === "draft";
  const canSend = isAuthor && can("submit_expense") && row.status === "submitted";
  const canDiscard = isAuthor && can("create_expense") && row.status === "draft";
  const isValidator = can("validate_expense") || can("pay_expense") || can("cancel_expense");
  const financeQueue = isValidator && can("view_trip_cost") && (row.status === "to_validate" || row.status === "validated");
  const missionExpense = !!row.mission && !row.trip;
  const infoRequest = pendingInfoRequest(history, row.status);
  const rejection = lastRejection(history, row.status);
  const busy = action.isPending || discard.isPending;
  const paymentMethod = PAYMENT_METHODS.find((m) => m.value === row.payment_method)?.label ?? row.payment_method;

  function fail(err: unknown) {
    const proposal = adjustmentProposal(err);
    setError(apiError(err));
    if (proposal) onProposal(proposal);
  }

  function run(kind: ExpenseAction) {
    setError("");
    setResult(null);
    action.mutate({ id: row!.id, action: kind, body: { comment } }, {
      onSuccess: (updated) => {
        setComment("");
        if (updated.status === "to_validate") {
          setResult({ tone: "success", text: "Dépense transmise à la validation." });
        } else if (updated.status === "submitted") {
          setResult({ tone: "warning", text: "Dépense soumise. Un justificatif est requis : joignez-le puis transmettez-la à la validation." });
        }
      },
      onError: fail,
    });
  }

  function doDiscard() {
    setError("");
    discard.mutate(row!.id, {
      onSuccess: () => { setConfirmDiscard(false); onDiscarded(); },
      onError: (err) => { setConfirmDiscard(false); fail(err); },
    });
  }

  return (
    <Modal open title="Détail de la dépense" onClose={onClose} className="sm:max-w-2xl">
      <div className="max-h-[72vh] space-y-4 overflow-y-auto pr-1 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          <p className="text-base font-semibold text-ink">{row.label}</p>
          <ExpenseStatusBadge status={row.status} />
          <NonCountableBadge expense={row} />
          <ReceiptMissingBadge expense={row} />
        </div>

        {notice && !result && <Notice tone="info">{notice}</Notice>}
        {result && <Notice tone={result.tone}>{result.text}</Notice>}
        <LateExpenseBanner late={row.late} />
        {infoRequest && (
          <Notice tone="warning">
            <b>Complément demandé par {infoRequest.user}</b>
            {infoRequest.comment ? ` : « ${infoRequest.comment} »` : ""}.
            {row.receipt_missing ? " Joignez le justificatif demandé" : " Complétez la dépense"} puis transmettez-la à nouveau à la validation.
          </Notice>
        )}
        {rejection && (
          <Notice tone="danger">
            <b>Rejetée par {rejection.user}</b>{rejection.reason ? ` : ${rejection.reason}` : ""}
          </Notice>
        )}

        <div className="grid grid-cols-2 gap-2 rounded-lg bg-surface2 p-3 text-xs text-muted">
          <Item label="Montant">{formatNumber(row.amount)} XOF</Item>
          <Item label="Date">{formatDate(row.date)}</Item>
          <Item label="Catégorie">{row.category_display}</Item>
          <Item label="Filiale">{row.subsidiary_name}</Item>
          <Item label="Véhicule">{row.vehicle_registration ?? "—"}</Item>
          <Item label="Course">{row.trip_destination ?? "—"}</Item>
          <Item label="Mission">{row.mission_code ?? "—"}</Item>
          <Item label="Chauffeur">{row.driver_name ?? "—"}</Item>
          <Item label="Centre de coût">{row.cost_center_label ?? "—"}</Item>
          <Item label="Fournisseur">{row.supplier || "—"}</Item>
          <Item label="Auteur">{row.author_name || "—"}</Item>
          <Item label="Enregistrée le">{formatDate(row.created_at, true)}</Item>
          {row.submitted_at && <Item label="Soumise le">{formatDate(row.submitted_at, true)}</Item>}
          {row.validated_at && (
            <Item label="Validée">{formatDate(row.validated_at, true)}{row.validated_by_name ? ` · ${row.validated_by_name}` : ""}</Item>
          )}
          <span className="col-span-2">Source : <b className="text-ink">{row.source_type_display}</b>
            {row.source_reference ? ` · ${row.source_reference}` : ""}
            {!row.is_countable && (row.status === "validated" || row.status === "paid")
              && " — comptée par sa source ou son ajustement, pas une seconde fois"}
            {!row.is_countable && !["validated", "paid"].includes(row.status)
              && " — pas encore comptée (seules les dépenses validées ou payées le sont)"}
          </span>
        </div>

        {row.status === "paid" && (
          <div className="grid grid-cols-2 gap-2 rounded-lg border border-line p-3 text-xs text-muted">
            <Item label="Payée le">{formatDate(row.paid_at, true)}</Item>
            <Item label="Payée par">{row.paid_by_name || "—"}</Item>
            <Item label="Mode de paiement">{paymentMethod || "—"}</Item>
            <Item label="Référence de paiement">{row.payment_reference || "—"}</Item>
            <Item label="Référence comptable" full>{row.accounting_reference || "—"}</Item>
          </div>
        )}

        {row.adjustment && (
          <Notice tone="info">
            Comptabilisée par un <b>ajustement financier</b> ({ADJUSTMENT_STATUS[row.adjustment.status] ?? row.adjustment.status})
            sur la période <b>{row.adjustment.posting_period}</b> — la période d&apos;origine n&apos;est pas réécrite.
            {can("view_trip_cost") && (
              <> <Link href="/finance" className="font-medium text-brand-600 hover:underline">Voir dans Finance › Ajustements</Link></>
            )}
          </Notice>
        )}

        {missionExpense && <ExpenseAllocations expense={row} />}

        <ExpenseAttachments expense={row} canWrite={can("create_expense")} canReadSettings={can("view_expense")} />

        <ExpenseHistory expenseId={row.id} />
      </div>

      <div className="space-y-2 border-t border-line pt-3">
        {(canSubmit || canSend) && (
          <Input value={comment} onChange={(e) => setComment(e.target.value)} className="h-9 text-xs"
                 placeholder="Commentaire pour le valideur (facultatif)" />
        )}
        {canSend && row.receipt_missing && (
          <p className="text-[11px] text-rose-700">Joignez d&apos;abord le justificatif requis pour pouvoir transmettre la dépense.</p>
        )}
        {financeQueue && (
          <p className="text-[11px] text-muted">
            {row.status === "to_validate" ? "La validation" : "Le paiement ou l'annulation"} se fait dans la file Finance.{" "}
            <Link href="/finance" className="inline-flex items-center gap-0.5 font-medium text-brand-600 hover:underline">
              Ouvrir Finance › Dépenses à valider <ArrowRight className="h-3 w-3" />
            </Link>
          </p>
        )}
        {error && <p className="rounded-lg bg-rose-50 px-3 py-2 text-xs text-rose-700 dark:bg-rose-500/10 dark:text-rose-300">{error}</p>}

        {confirmDiscard ? (
          <div className="flex flex-wrap items-center justify-end gap-2">
            <p className="mr-auto text-xs text-muted">Le brouillon passera « Annulée » (tracé) ; rien n&apos;est effacé.</p>
            <Button variant="secondary" size="sm" onClick={() => setConfirmDiscard(false)}>Garder</Button>
            <Button variant="danger" size="sm" disabled={busy} onClick={doDiscard}>Abandonner le brouillon</Button>
          </div>
        ) : (
          <div className="flex flex-wrap justify-end gap-2">
            {canDiscard && (
              <Button variant="ghost" size="sm" className="mr-auto text-rose-600" disabled={busy} onClick={() => setConfirmDiscard(true)}>
                <Trash2 className="h-3.5 w-3.5" /> Abandonner le brouillon
              </Button>
            )}
            <Button variant="secondary" size="sm" onClick={onClose}>Fermer</Button>
            {canEdit && (
              <Button variant="secondary" size="sm" disabled={busy} onClick={() => onEdit(row)}>
                <Pencil className="h-3.5 w-3.5" /> {row.status === "to_validate" ? "Corriger le centre de coût" : "Modifier"}
              </Button>
            )}
            {canSubmit && (
              <Button size="sm" disabled={busy} onClick={() => run("submit")}>
                {action.isPending ? <Spinner className="h-3.5 w-3.5 border-white/50 border-t-white" /> : <Send className="h-3.5 w-3.5" />}
                Soumettre
              </Button>
            )}
            {canSend && (
              <Button size="sm" disabled={busy || row.receipt_missing} onClick={() => run("send-for-validation")}>
                {action.isPending ? <Spinner className="h-3.5 w-3.5 border-white/50 border-t-white" /> : <Send className="h-3.5 w-3.5" />}
                Transmettre à la validation
              </Button>
            )}
          </div>
        )}
      </div>
    </Modal>
  );
}
