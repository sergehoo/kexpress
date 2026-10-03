"use client";

import { useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { FileUp, X } from "lucide-react";

import { Button, Input, Label, Select, Spinner } from "@/components/ui";
import { Modal } from "@/components/Modal";
import { api, apiError } from "@/lib/api";

/** Formats acceptés (le serveur contrôle le CONTENU du fichier, pas seulement l'extension). */
const ACCEPT = ".pdf,.jpg,.jpeg,.png,.webp,application/pdf,image/jpeg,image/png,image/webp";
const ACCEPTED_TYPES = ["application/pdf", "image/jpeg", "image/png", "image/webp"];
const MAX_MB = 10;

export const VEHICLE_DOC_TYPES = [
  { value: "registration", label: "Carte grise" },
  { value: "insurance", label: "Assurance" },
  { value: "technical_inspection", label: "Visite technique" },
  { value: "vignette", label: "Vignette" },
  { value: "transport_authorization", label: "Autorisation de transport" },
  { value: "lease_contract", label: "Contrat de location / leasing" },
  { value: "acquisition_invoice", label: "Facture d'acquisition" },
  { value: "other", label: "Autre" },
];

export const DRIVER_DOC_TYPES = [
  { value: "license", label: "Permis de conduire" },
  { value: "id_card", label: "Carte nationale d'identité (CNI)" },
  { value: "training", label: "Attestation de formation" },
  { value: "medical", label: "Certificat médical" },
  { value: "habilitation", label: "Habilitation professionnelle" },
  { value: "contract", label: "Contrat" },
  { value: "other", label: "Autre" },
];

/** Ajout d'un document avec sa pièce (glisser-déposer, progression, erreurs explicites). */
export function DocumentForm({
  open, onClose, resource, parentField, parentId, types,
}: {
  open: boolean;
  onClose: () => void;
  resource: "vehicle-documents" | "driver-documents";
  parentField: "vehicle" | "driver";
  parentId: string;
  types: { value: string; label: string }[];
}) {
  const qc = useQueryClient();
  const input = useRef<HTMLInputElement>(null);
  const [values, setValues] = useState({ doc_type: "", number: "", issue_date: "", expiry_date: "" });
  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);
  const [error, setError] = useState("");

  function pick(f: File | undefined) {
    setError("");
    if (!f) return;
    if (!ACCEPTED_TYPES.includes(f.type)) {
      setError("Format non accepté : PDF, JPG, JPEG, PNG ou WEBP uniquement.");
      return;
    }
    if (f.size > MAX_MB * 1024 * 1024) {
      setError(`Fichier trop volumineux (${(f.size / 1024 / 1024).toFixed(1)} Mo) : ${MAX_MB} Mo au maximum.`);
      return;
    }
    setFile(f);
  }

  function close() {
    setValues({ doc_type: "", number: "", issue_date: "", expiry_date: "" });
    setFile(null); setProgress(null); setError("");
    onClose();
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    const form = new FormData();
    form.append(parentField, parentId);
    Object.entries(values).forEach(([k, v]) => { if (v) form.append(k, v); });
    if (file) form.append("file", file);
    try {
      setProgress(0);
      await api.post(`/${resource}/`, form, {
        onUploadProgress: (p) => setProgress(p.total ? Math.round((p.loaded / p.total) * 100) : null),
      });
      await qc.invalidateQueries({ queryKey: [resource] });
      close();
    } catch (err) {
      setProgress(null);
      setError(apiError(err));
    }
  }

  const set = (k: keyof typeof values) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setValues((v) => ({ ...v, [k]: e.target.value }));

  return (
    <Modal open={open} title="Nouveau document" onClose={close} className="sm:max-w-lg">
      <form onSubmit={submit} className="space-y-3">
        <div className="grid grid-cols-2 gap-3">
          <div className="col-span-2">
            <Label>Type *</Label>
            <Select value={values.doc_type} onChange={set("doc_type")} required>
              <option value="">—</option>
              {types.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
            </Select>
          </div>
          <div className="col-span-2">
            <Label>Numéro</Label>
            <Input value={values.number} onChange={set("number")} />
          </div>
          <div>
            <Label>Date d&apos;émission</Label>
            <Input type="date" value={values.issue_date} onChange={set("issue_date")} />
          </div>
          <div>
            <Label>Date d&apos;expiration</Label>
            <Input type="date" value={values.expiry_date} onChange={set("expiry_date")} />
          </div>
        </div>

        <div
          role="button" tabIndex={0}
          onClick={() => input.current?.click()}
          onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") input.current?.click(); }}
          onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => { e.preventDefault(); setDragging(false); pick(e.dataTransfer.files?.[0]); }}
          className={`flex cursor-pointer flex-col items-center gap-1 rounded-xl border-2 border-dashed px-4 py-5 text-center text-sm transition ${
            dragging ? "border-brand-400 bg-brand-500/5" : "border-line hover:border-brand-300"}`}
        >
          <FileUp className="h-5 w-5 text-faint" />
          {file ? (
            <span className="flex items-center gap-2 font-medium text-ink">
              <span className="max-w-[16rem] truncate">{file.name}</span>
              <button type="button" aria-label="Retirer le fichier" className="text-muted hover:text-ink"
                onClick={(e) => { e.stopPropagation(); setFile(null); if (input.current) input.current.value = ""; }}>
                <X className="h-4 w-4" />
              </button>
            </span>
          ) : (
            <span className="text-muted">Glissez la pièce ici ou <span className="font-medium text-brand-600">parcourez</span></span>
          )}
          <span className="text-[11px] text-faint">PDF, JPG, PNG ou WEBP · {MAX_MB} Mo max.</span>
          <input ref={input} type="file" accept={ACCEPT} className="hidden"
            onChange={(e) => pick(e.target.files?.[0])} />
        </div>

        {progress !== null && (
          <div className="h-1.5 w-full overflow-hidden rounded-full bg-line" aria-label="Progression du téléversement">
            <div className="h-full bg-brand-500 transition-all" style={{ width: `${progress}%` }} />
          </div>
        )}
        {error && <p className="rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-700 dark:bg-rose-500/10 dark:text-rose-300">{error}</p>}

        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" variant="secondary" onClick={close}>Annuler</Button>
          <Button type="submit" disabled={progress !== null}>
            {progress !== null ? <Spinner className="h-4 w-4 border-white/50 border-t-white" /> : "Enregistrer"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
