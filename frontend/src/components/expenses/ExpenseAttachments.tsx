"use client";

import { useRef, useState } from "react";
import { FileText, Paperclip, Trash2, Upload } from "lucide-react";

import { Button, Select, Spinner } from "@/components/ui";
import { SecureFileLink } from "@/components/SecureFileLink";
import { money } from "@/components/finance/RealCostPanel";
import { apiError } from "@/lib/api";
import {
  ATTACHMENT_KINDS, useDeleteAttachment, useFinanceSettings, useUploadAttachment,
  type WorkflowExpense,
} from "@/lib/financeF2";
import { formatDate } from "@/lib/utils";

import { EDITABLE } from "./ExpenseBadges";

/** Formats et taille acceptés — miroir de `AttachmentUploadSerializer` : le contrôle local
 *  évite d'envoyer 30 Mo pour un refus, l'API reste juge. */
const ACCEPT = ".pdf,.jpg,.jpeg,.png,.webp,.heic,.heif";
const EXTENSIONS = ACCEPT.split(",");
const MAX_BYTES = 10 * 1024 * 1024;

function size(bytes?: number) {
  if (!bytes) return "";
  return bytes >= 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} Mo` : `${Math.max(1, Math.round(bytes / 1024))} Ko`;
}

/** Règle du seuil de justificatif, telle que la Finance l'a paramétrée. */
export function ReceiptRule({ expense, enabled }: { expense: WorkflowExpense; enabled: boolean }) {
  const { data: settings } = useFinanceSettings(enabled);
  const threshold = settings?.receipt_required_from;
  let rule: string | null = null;
  if (settings) {
    rule = threshold == null
      ? "Aucun justificatif n'est exigé automatiquement ; la Finance peut en demander un."
      : Number(threshold) === 0
        ? "Justificatif obligatoire pour toute dépense."
        : `Justificatif obligatoire à partir de ${money(threshold, settings.currency || "XOF")}.`;
  }
  return (
    <div className="space-y-1 text-[11px]">
      {rule && <p className="text-faint">{rule}</p>}
      {expense.receipt_required && (
        <p className="text-amber-700">La Finance a exigé un justificatif pour cette dépense.</p>
      )}
      {expense.receipt_missing && (
        <p className="font-medium text-rose-700">
          Justificatif requis manquant : joignez-le avant de transmettre la dépense à la validation.
        </p>
      )}
    </div>
  );
}

/** Justificatifs d'une dépense : liste (ouverture par URL signée uniquement), dépôt, retrait.
 *
 *  Le retrait n'est offert que tant que la dépense est en circuit (brouillon, soumise, à
 *  valider) : une fois validée, la pièce fait foi et l'API la conserve. */
export function ExpenseAttachments({ expense, canWrite, canReadSettings }: {
  expense: WorkflowExpense;
  /** Droit `create_expense` : dépôt et retrait des pièces. */
  canWrite: boolean;
  canReadSettings: boolean;
}) {
  const [kind, setKind] = useState("receipt");
  const [error, setError] = useState("");
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const upload = useUploadAttachment("expenses");
  const remove = useDeleteAttachment();

  const editable = EDITABLE.includes(expense.status);
  const canUpload = canWrite && expense.status !== "rejected" && expense.status !== "cancelled";
  const canDelete = canWrite && editable;
  const kindLabel = (value: string) => ATTACHMENT_KINDS.find((k) => k.value === value)?.label ?? value;

  function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = ""; // permet de redéposer le même fichier après un refus
    setError("");
    if (!file) return;
    const ext = file.name.includes(".") ? `.${file.name.split(".").pop()!.toLowerCase()}` : "";
    if (!EXTENSIONS.includes(ext)) {
      setError("Format non accepté (PDF, JPEG, PNG, WebP, HEIC).");
      return;
    }
    if (file.size > MAX_BYTES) {
      setError("Fichier trop volumineux (10 Mo maximum).");
      return;
    }
    upload.mutate({ id: expense.id, file, kind }, { onError: (err) => setError(apiError(err)) });
  }

  function onDelete(attachmentId: string) {
    setError("");
    remove.mutate({ expenseId: expense.id, attachmentId }, {
      onSuccess: () => setConfirmId(null),
      onError: (err) => { setConfirmId(null); setError(apiError(err)); },
    });
  }

  const attachments = expense.attachments ?? [];

  return (
    <section className="space-y-2">
      <div className="flex items-center justify-between">
        <h4 className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-faint">
          <Paperclip className="h-3.5 w-3.5" /> Justificatifs
        </h4>
        <span className="text-[11px] text-faint">{attachments.length + (expense.receipt ? 1 : 0)} pièce(s)</span>
      </div>
      <ReceiptRule expense={expense} enabled={canReadSettings} />

      {attachments.length === 0 && !expense.receipt ? (
        <p className="rounded-lg border border-dashed border-line px-3 py-3 text-center text-xs text-faint">Aucun justificatif joint.</p>
      ) : (
        <ul className="divide-y divide-line rounded-lg border border-line">
          {expense.receipt && (
            <li className="flex items-center gap-2 px-3 py-2 text-xs">
              <FileText className="h-4 w-4 shrink-0 text-faint" />
              <span className="min-w-0 flex-1 truncate text-muted">Justificatif d&apos;origine</span>
              <SecureFileLink url={expense.receipt} label="Ouvrir" />
            </li>
          )}
          {attachments.map((a) => (
            <li key={a.id} className="flex items-center gap-2 px-3 py-2 text-xs">
              <FileText className="h-4 w-4 shrink-0 text-faint" />
              <div className="min-w-0 flex-1">
                <p className="truncate font-medium text-ink" title={a.name}>{a.name || "Pièce"}</p>
                <p className="text-[11px] text-faint">
                  {a.kind_display || kindLabel(a.kind)}
                  {a.size ? ` · ${size(a.size)}` : ""}
                  {a.uploaded_at ? ` · ${formatDate(a.uploaded_at, true)}` : ""}
                </p>
              </div>
              {a.url ? <SecureFileLink url={a.url} label="Ouvrir" />
                : <span className="text-[11px] text-faint">Accès restreint</span>}
              {canDelete && (confirmId === a.id ? (
                <span className="flex items-center gap-1">
                  <Button size="sm" variant="danger" disabled={remove.isPending} onClick={() => onDelete(a.id)}>Retirer</Button>
                  <Button size="sm" variant="ghost" onClick={() => setConfirmId(null)}>Non</Button>
                </span>
              ) : (
                <button type="button" onClick={() => setConfirmId(a.id)} title="Retirer ce justificatif"
                        aria-label="Retirer ce justificatif"
                        className="rounded-md p-1.5 text-muted hover:bg-surface2 hover:text-rose-600">
                  <Trash2 className="h-4 w-4" />
                </button>
              ))}
            </li>
          ))}
        </ul>
      )}

      {canUpload && (
        <div className="flex flex-wrap items-center gap-2">
          <Select value={kind} onChange={(e) => setKind(e.target.value)} className="h-8 w-44 text-xs" aria-label="Type de justificatif">
            {ATTACHMENT_KINDS.map((k) => <option key={k.value} value={k.value}>{k.label}</option>)}
          </Select>
          <input ref={fileInput} type="file" accept={ACCEPT} className="hidden" onChange={onFile} />
          <Button size="sm" variant="secondary" disabled={upload.isPending} onClick={() => fileInput.current?.click()}>
            {upload.isPending ? <Spinner className="h-3.5 w-3.5" /> : <Upload className="h-3.5 w-3.5" />}
            Joindre un fichier
          </Button>
          <span className="text-[10px] text-faint">PDF, JPEG, PNG, WebP, HEIC · 10 Mo max.</span>
        </div>
      )}
      {error && <p className="text-xs text-rose-600">{error}</p>}
    </section>
  );
}
