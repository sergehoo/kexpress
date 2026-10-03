"use client";

import { useRef, useState } from "react";
import { Camera, Check, CheckCircle2, Clock, FileText, Plus, Trash2, X } from "lucide-react";

import { Modal } from "@/components/Modal";
import { SecureFileLink } from "@/components/SecureFileLink";
import { Button, Input, Label, Select, Spinner } from "@/components/ui";
import {
  ANOMALY_SEVERITY_LABEL, carPlanError, CONDITION_LABEL, DEFAULT_DOCUMENTS, DEFAULT_EQUIPMENT, TYRE_POSITIONS,
  TYRE_STATES, useCreateInspection, useSignInspection, useUploadInspectionPhoto,
  type Comparison, type Condition, type Inspection, type NewInspection,
} from "@/lib/carplan";
import { cn, formatNumber } from "@/lib/utils";

import { FormError, formatDateTime, km, Notice, PhotoLightbox, SecureImage, Textarea, ToneBadge } from "./shared";

const CONDITIONS = Object.entries(CONDITION_LABEL) as [Condition, string][];
const CONDITION_TONE: Record<Condition, "green" | "amber" | "red"> = { good: "green", fair: "amber", poor: "red" };

function tyreLabel(key: string) {
  return TYRE_POSITIONS.find((t) => t.key === key)?.label ?? key.replace(/_/g, " ");
}

// --- Formulaire structuré de création -------------------------------------------------------

type Row<T> = T & { key: number };

/** État des lieux (remise ou restitution) réalisé par le gestionnaire. Le bénéficiaire le
 *  valide ensuite depuis son espace, puis le gestionnaire ; la double validation émet le PV. */
export function InspectionForm({ assignmentId, kind, defaultMileage, template, onClose, onCreated }: {
  assignmentId: string;
  kind: "handover" | "return";
  defaultMileage?: number | null;
  /** État des lieux de remise validé : préremplit équipements, documents, pneus et anomalies connues. */
  template?: Inspection | null;
  onClose: () => void;
  onCreated: (inspection: Inspection) => void;
}) {
  const create = useCreateInspection();
  const seq = useRef(1000);
  const next = () => ++seq.current;
  const [mileage, setMileage] = useState(defaultMileage != null ? String(defaultMileage) : "");
  const [energy, setEnergy] = useState(template ? template.energy_level_pct : 100);
  const [exterior, setExterior] = useState<Condition>(template?.exterior_condition ?? "good");
  const [interior, setInterior] = useState<Condition>(template?.interior_condition ?? "good");
  const [exteriorNotes, setExteriorNotes] = useState("");
  const [interiorNotes, setInteriorNotes] = useState("");
  const [tyres, setTyres] = useState<Record<string, string>>(() => {
    const base = Object.fromEntries(TYRE_POSITIONS.map((t) => [t.key, "bon"]));
    return template?.tyres && Object.keys(template.tyres).length ? { ...base, ...template.tyres } : base;
  });
  const [equipment, setEquipment] = useState<Row<{ item: string; present: boolean }>[]>(() =>
    (template?.equipment?.length ? template.equipment.map((e) => e.item) : DEFAULT_EQUIPMENT)
      .map((item, i) => ({ key: i, item, present: true })));
  const [documents, setDocuments] = useState<Row<{ item: string; handed: boolean }>[]>(() =>
    (template?.documents?.length ? template.documents.map((d) => d.item) : DEFAULT_DOCUMENTS)
      .map((item, i) => ({ key: 100 + i, item, handed: true })));
  const [anomalies, setAnomalies] = useState<Row<{ zone: string; description: string; severity: string }>[]>(() =>
    (template?.anomalies ?? []).map((a, i) => ({
      key: 500 + i, zone: a.zone ?? "", description: a.description ?? "", severity: a.severity ?? "minor",
    })));
  const [observations, setObservations] = useState("");
  const [newEquipment, setNewEquipment] = useState("");
  const [newDocument, setNewDocument] = useState("");
  const [error, setError] = useState("");

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    const m = Number(mileage);
    if (mileage.trim() === "" || !Number.isInteger(m) || m < 0) {
      setError("Kilométrage relevé obligatoire (nombre entier positif).");
      return;
    }
    if (!(energy >= 0 && energy <= 100)) {
      setError("Niveau de carburant / batterie entre 0 et 100 %.");
      return;
    }
    const body: NewInspection = {
      kind,
      mileage: m,
      energy_level_pct: Math.round(energy),
      exterior_condition: exterior,
      interior_condition: interior,
      exterior_notes: exteriorNotes.trim(),
      interior_notes: interiorNotes.trim(),
      tyres,
      equipment: equipment.filter((r) => r.item.trim()).map(({ item, present }) => ({ item: item.trim(), present })),
      documents: documents.filter((r) => r.item.trim()).map(({ item, handed }) => ({ item: item.trim(), handed })),
      anomalies: anomalies
        .filter((a) => a.zone.trim() || a.description.trim())
        .map(({ zone, description, severity }) => ({ zone: zone.trim(), description: description.trim(), severity })),
      observations: observations.trim(),
    };
    create.mutate({ assignmentId, body }, {
      onSuccess: (inspection) => onCreated(inspection),
      onError: (err) => setError(carPlanError(err)),
    });
  }

  return (
    <Modal open onClose={onClose} className="max-h-[94vh] max-w-2xl overflow-y-auto"
           title={kind === "handover" ? "État des lieux de remise" : "État des lieux de restitution"}>
      <form onSubmit={submit} className="space-y-5">
        <Notice tone="info">
          Le bénéficiaire valide cet état des lieux depuis son espace « Mon véhicule », puis vous le validez à votre
          tour : la double validation émet le procès-verbal et {kind === "handover" ? "active l'attribution" : "enregistre la restitution"}.
          Les photos s&apos;ajoutent une fois l&apos;état des lieux créé.
        </Notice>

        <fieldset className="grid gap-3 sm:grid-cols-2">
          <div>
            <Label htmlFor="insp-km">Kilométrage relevé (km)</Label>
            <Input id="insp-km" type="number" min={0} inputMode="numeric" required value={mileage}
                   onChange={(e) => setMileage(e.target.value)} />
          </div>
          <div>
            <Label htmlFor="insp-energy">Carburant / batterie : {energy} %</Label>
            <div className="flex items-center gap-2">
              <input id="insp-energy" type="range" min={0} max={100} step={5} value={energy}
                     onChange={(e) => setEnergy(Number(e.target.value))} className="h-10 flex-1 accent-brand-600" />
              <Input type="number" min={0} max={100} value={energy} aria-label="Niveau en %"
                     onChange={(e) => setEnergy(Number(e.target.value))} className="w-20" />
            </div>
          </div>
        </fieldset>

        <fieldset className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-2">
            <Label htmlFor="insp-ext">État extérieur</Label>
            <Select id="insp-ext" value={exterior} onChange={(e) => setExterior(e.target.value as Condition)}>
              {CONDITIONS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </Select>
            <Textarea rows={2} placeholder="Carrosserie, vitres, optiques…" value={exteriorNotes}
                      onChange={(e) => setExteriorNotes(e.target.value)} aria-label="Observations extérieur" />
          </div>
          <div className="space-y-2">
            <Label htmlFor="insp-int">État intérieur</Label>
            <Select id="insp-int" value={interior} onChange={(e) => setInterior(e.target.value as Condition)}>
              {CONDITIONS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </Select>
            <Textarea rows={2} placeholder="Sièges, tableau de bord, propreté…" value={interiorNotes}
                      onChange={(e) => setInteriorNotes(e.target.value)} aria-label="Observations intérieur" />
          </div>
        </fieldset>

        <fieldset>
          <legend className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Pneumatiques</legend>
          <div className="grid gap-2 sm:grid-cols-2">
            {TYRE_POSITIONS.map((t) => (
              <label key={t.key} className="flex items-center justify-between gap-2 rounded-lg border border-line px-3 py-1.5 text-sm">
                <span className="text-ink">{t.label}</span>
                <select value={tyres[t.key] ?? "bon"} onChange={(e) => setTyres({ ...tyres, [t.key]: e.target.value })}
                        className="h-8 rounded-md border border-line bg-surface px-2 text-xs">
                  {TYRE_STATES.map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
              </label>
            ))}
          </div>
        </fieldset>

        <fieldset>
          <legend className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Équipements et accessoires</legend>
          <ul className="space-y-1.5">
            {equipment.map((row) => (
              <li key={row.key} className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={row.present} className="h-4 w-4 accent-brand-600"
                       aria-label={`${row.item} présent`}
                       onChange={(e) => setEquipment(equipment.map((r) => r.key === row.key ? { ...r, present: e.target.checked } : r))} />
                <span className={cn("flex-1", !row.present && "text-muted line-through")}>{row.item}</span>
                <span className="text-[11px] text-faint">{row.present ? "présent" : "absent"}</span>
                <button type="button" aria-label={`Retirer ${row.item}`} className="rounded p-1 text-faint hover:text-rose-600"
                        onClick={() => setEquipment(equipment.filter((r) => r.key !== row.key))}>
                  <X className="h-3.5 w-3.5" />
                </button>
              </li>
            ))}
          </ul>
          <div className="mt-2 flex gap-2">
            <Input value={newEquipment} onChange={(e) => setNewEquipment(e.target.value)} placeholder="Autre équipement"
                   className="h-8 text-xs" />
            <Button type="button" size="sm" variant="secondary" disabled={!newEquipment.trim()}
                    onClick={() => { setEquipment([...equipment, { key: next(), item: newEquipment.trim(), present: true }]); setNewEquipment(""); }}>
              <Plus className="h-3.5 w-3.5" /> Ajouter
            </Button>
          </div>
        </fieldset>

        <fieldset>
          <legend className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
            Documents {kind === "handover" ? "remis" : "restitués"}
          </legend>
          <ul className="space-y-1.5">
            {documents.map((row) => (
              <li key={row.key} className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={row.handed} className="h-4 w-4 accent-brand-600"
                       aria-label={`${row.item} remis`}
                       onChange={(e) => setDocuments(documents.map((r) => r.key === row.key ? { ...r, handed: e.target.checked } : r))} />
                <span className={cn("flex-1", !row.handed && "text-muted line-through")}>{row.item}</span>
                <span className="text-[11px] text-faint">{row.handed ? "remis" : "non remis"}</span>
                <button type="button" aria-label={`Retirer ${row.item}`} className="rounded p-1 text-faint hover:text-rose-600"
                        onClick={() => setDocuments(documents.filter((r) => r.key !== row.key))}>
                  <X className="h-3.5 w-3.5" />
                </button>
              </li>
            ))}
          </ul>
          <div className="mt-2 flex gap-2">
            <Input value={newDocument} onChange={(e) => setNewDocument(e.target.value)} placeholder="Autre document"
                   className="h-8 text-xs" />
            <Button type="button" size="sm" variant="secondary" disabled={!newDocument.trim()}
                    onClick={() => { setDocuments([...documents, { key: next(), item: newDocument.trim(), handed: true }]); setNewDocument(""); }}>
              <Plus className="h-3.5 w-3.5" /> Ajouter
            </Button>
          </div>
        </fieldset>

        <fieldset>
          <legend className="mb-2 flex w-full items-center justify-between text-xs font-semibold uppercase tracking-wide text-muted">
            Anomalies constatées
          </legend>
          {template && anomalies.length > 0 && (
            <p className="mb-2 text-[11px] text-faint">Anomalies relevées à la remise, reprises pour comparaison : retirez celles qui ont été réparées.</p>
          )}
          <div className="space-y-2">
            {anomalies.map((row) => (
              <div key={row.key} className="grid gap-2 rounded-lg border border-line p-2 sm:grid-cols-[1fr_2fr_auto_auto]">
                <Input value={row.zone} placeholder="Zone (ex. pare-chocs avant)" className="h-9 text-xs" aria-label="Zone"
                       onChange={(e) => setAnomalies(anomalies.map((r) => r.key === row.key ? { ...r, zone: e.target.value } : r))} />
                <Input value={row.description} placeholder="Description (ex. rayure)" className="h-9 text-xs" aria-label="Description"
                       onChange={(e) => setAnomalies(anomalies.map((r) => r.key === row.key ? { ...r, description: e.target.value } : r))} />
                <Select value={row.severity} className="h-9 text-xs sm:w-28" aria-label="Gravité"
                        onChange={(e) => setAnomalies(anomalies.map((r) => r.key === row.key ? { ...r, severity: e.target.value } : r))}>
                  {Object.entries(ANOMALY_SEVERITY_LABEL).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                </Select>
                <button type="button" aria-label="Retirer l'anomalie" className="justify-self-end rounded p-2 text-faint hover:text-rose-600"
                        onClick={() => setAnomalies(anomalies.filter((r) => r.key !== row.key))}>
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>
            ))}
          </div>
          <Button type="button" size="sm" variant="secondary" className="mt-2"
                  onClick={() => setAnomalies([...anomalies, { key: next(), zone: "", description: "", severity: "minor" }])}>
            <Plus className="h-3.5 w-3.5" /> Ajouter une anomalie
          </Button>
        </fieldset>

        <div>
          <Label htmlFor="insp-obs">Observations générales</Label>
          <Textarea id="insp-obs" value={observations} onChange={(e) => setObservations(e.target.value)} />
        </div>

        <FormError message={error} />
        <div className="flex justify-end gap-2 border-t border-line pt-3">
          <Button type="button" variant="secondary" onClick={onClose}>Annuler</Button>
          <Button type="submit" disabled={create.isPending}>
            {create.isPending && <Spinner className="h-4 w-4" />} Enregistrer l&apos;état des lieux
          </Button>
        </div>
      </form>
    </Modal>
  );
}

// --- Affichage, validation, photos ------------------------------------------------------------

function SignatureLine({ label, at, by }: { label: string; at: string | null; by?: string | null }) {
  return (
    <div className="flex items-center gap-1.5 text-xs">
      {at ? <CheckCircle2 className="h-4 w-4 text-emerald-600" /> : <Clock className="h-4 w-4 text-amber-500" />}
      <span className="text-muted">{label} :</span>
      <span className={at ? "text-ink" : "text-amber-700 dark:text-amber-300"}>
        {at ? `validé le ${formatDateTime(at)}${by ? ` par ${by}` : ""}` : "en attente"}
      </span>
    </div>
  );
}

/** Détail d'un état des lieux. `as` : partie qui consulte (gestionnaire ou bénéficiaire) — elle
 *  détermine la route de validation et de photo. */
export function InspectionCard({ inspection, as, canSign, canAddPhoto, defaultOpen = false, onFlash }: {
  inspection: Inspection;
  as: "manager" | "beneficiary";
  canSign: boolean;
  canAddPhoto: boolean;
  defaultOpen?: boolean;
  onFlash?: (text: string, tone?: "success" | "danger") => void;
}) {
  const sign = useSignInspection(as);
  const upload = useUploadInspectionPhoto(as);
  const [open, setOpen] = useState(defaultOpen);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [zone, setZone] = useState("");
  const [caption, setCaption] = useState("");
  const [viewing, setViewing] = useState<number | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const i = inspection;

  const mineToSign = as === "beneficiary" ? !i.employee_signed_at : !!i.employee_signed_at && !i.manager_signed_at;
  const showSign = canSign && mineToSign;

  function doSign() {
    setError("");
    sign.mutate(i.id, {
      onSuccess: (res) => {
        setConfirming(false);
        onFlash?.(res.is_signed
          ? "État des lieux validé par les deux parties : le procès-verbal est émis."
          : "Validation enregistrée.", "success");
      },
      onError: (err) => { setConfirming(false); setError(carPlanError(err)); },
    });
  }

  function doUpload() {
    if (!file) return;
    setError("");
    upload.mutate({ inspectionId: i.id, file, zone: zone.trim(), caption: caption.trim() }, {
      onSuccess: () => {
        setFile(null); setZone(""); setCaption("");
        if (fileRef.current) fileRef.current.value = "";
        onFlash?.("Photo ajoutée.", "success");
      },
      onError: (err) => setError(carPlanError(err)),
    });
  }

  return (
    <div className="rounded-xl border border-line bg-surface">
      <button type="button" onClick={() => setOpen(!open)}
              className="flex w-full flex-wrap items-center gap-2 px-4 py-3 text-left" aria-expanded={open}>
        <span className="text-sm font-semibold text-ink">{i.kind_display}</span>
        <span className="text-xs text-muted">{formatDateTime(i.performed_at)} · {i.vehicle_registration} · {km(i.mileage)}</span>
        <span className="ml-auto">
          {i.is_signed
            ? <ToneBadge tone="green" label="Validé — PV émis" />
            : <ToneBadge tone="amber" label={i.employee_signed_at ? "Attente gestionnaire" : "Attente bénéficiaire"} />}
        </span>
      </button>

      {open && (
        <div className="space-y-4 border-t border-line px-4 py-3">
          <div className="space-y-1">
            <SignatureLine label="Bénéficiaire" at={i.employee_signed_at} />
            <SignatureLine label="Gestionnaire" at={i.manager_signed_at} by={i.manager_signed_by_name} />
            {i.performed_by_name && <p className="text-[11px] text-faint">Réalisé par {i.performed_by_name}</p>}
          </div>

          <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
            <div><dt className="text-[11px] text-faint">Kilométrage</dt><dd className="font-medium text-ink">{km(i.mileage)}</dd></div>
            <div><dt className="text-[11px] text-faint">Carburant / batterie</dt><dd className="font-medium text-ink">{formatNumber(i.energy_level_pct)} %</dd></div>
            <div><dt className="text-[11px] text-faint">Extérieur</dt>
              <dd><ToneBadge tone={CONDITION_TONE[i.exterior_condition] ?? "slate"} label={CONDITION_LABEL[i.exterior_condition] ?? i.exterior_condition} /></dd></div>
            <div><dt className="text-[11px] text-faint">Intérieur</dt>
              <dd><ToneBadge tone={CONDITION_TONE[i.interior_condition] ?? "slate"} label={CONDITION_LABEL[i.interior_condition] ?? i.interior_condition} /></dd></div>
          </dl>
          {(i.exterior_notes || i.interior_notes) && (
            <div className="grid gap-2 text-xs text-muted sm:grid-cols-2">
              {i.exterior_notes && <p><span className="font-medium text-ink">Extérieur :</span> {i.exterior_notes}</p>}
              {i.interior_notes && <p><span className="font-medium text-ink">Intérieur :</span> {i.interior_notes}</p>}
            </div>
          )}

          <div className="grid gap-4 sm:grid-cols-3">
            <div>
              <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-faint">Pneumatiques</p>
              {Object.keys(i.tyres ?? {}).length === 0 ? <p className="text-xs text-faint">—</p> : (
                <ul className="space-y-0.5 text-xs">
                  {Object.entries(i.tyres).map(([k, v]) => (
                    <li key={k} className="flex justify-between gap-2"><span className="text-muted">{tyreLabel(k)}</span><span className="text-ink">{v}</span></li>
                  ))}
                </ul>
              )}
            </div>
            <div>
              <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-faint">Équipements</p>
              {i.equipment.length === 0 ? <p className="text-xs text-faint">—</p> : (
                <ul className="space-y-0.5 text-xs">
                  {i.equipment.map((e, idx) => (
                    <li key={idx} className="flex items-center gap-1.5">
                      {e.present ? <Check className="h-3.5 w-3.5 text-emerald-600" /> : <X className="h-3.5 w-3.5 text-rose-600" />}
                      <span className={e.present ? "text-ink" : "text-muted"}>{e.item}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
            <div>
              <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-faint">Documents</p>
              {i.documents.length === 0 ? <p className="text-xs text-faint">—</p> : (
                <ul className="space-y-0.5 text-xs">
                  {i.documents.map((d, idx) => (
                    <li key={idx} className="flex items-center gap-1.5">
                      {d.handed ? <Check className="h-3.5 w-3.5 text-emerald-600" /> : <X className="h-3.5 w-3.5 text-rose-600" />}
                      <span className={d.handed ? "text-ink" : "text-muted"}>{d.item}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>

          <div>
            <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-faint">Anomalies</p>
            {i.anomalies.length === 0 ? <p className="text-xs text-muted">Aucune anomalie relevée.</p> : (
              <ul className="space-y-1 text-xs">
                {i.anomalies.map((a, idx) => (
                  <li key={idx} className="flex flex-wrap items-center gap-1.5">
                    <span className="font-medium text-ink">{a.zone || "—"}</span>
                    <span className="text-muted">{a.description}</span>
                    {a.severity && <span className="rounded-full bg-surface2 px-2 py-0.5 text-[10px] text-muted">{ANOMALY_SEVERITY_LABEL[a.severity] ?? a.severity}</span>}
                  </li>
                ))}
              </ul>
            )}
          </div>
          {i.observations && <p className="text-xs text-muted"><span className="font-medium text-ink">Observations :</span> {i.observations}</p>}

          <div>
            <p className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-faint">Photos ({i.photos.length})</p>
            {viewing !== null && (
              <PhotoLightbox
                photos={i.photos.map((p) => ({
                  url: p.image, alt: p.caption || p.zone || "Photo d'état des lieux",
                  caption: [p.zone, p.caption].filter(Boolean).join(" — ") || undefined,
                }))}
                index={viewing} onIndex={setViewing} onClose={() => setViewing(null)} />
            )}
            {i.photos.length === 0 ? <p className="text-xs text-muted">Aucune photo.</p> : (
              <div className="grid grid-cols-3 gap-2 sm:grid-cols-5">
                {i.photos.map((p, index) => (
                  <figure key={p.id} className="space-y-1">
                    <SecureImage url={p.image} alt={p.caption || p.zone || "Photo d'état des lieux"} className="aspect-square w-full"
                                 onOpen={() => setViewing(index)} />
                    {(p.zone || p.caption) && (
                      <figcaption className="truncate text-[10px] text-muted" title={[p.zone, p.caption].filter(Boolean).join(" — ")}>
                        {[p.zone, p.caption].filter(Boolean).join(" — ")}
                      </figcaption>
                    )}
                  </figure>
                ))}
              </div>
            )}
            {canAddPhoto && !i.is_signed && (
              <div className="mt-3 space-y-2 rounded-lg border border-dashed border-line p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <label className="inline-flex cursor-pointer items-center gap-2 rounded-lg border border-line bg-surface px-3 py-2 text-xs font-medium text-ink hover:bg-surface2">
                    <Camera className="h-4 w-4" /> {file ? file.name : "Prendre / choisir une photo"}
                    <input ref={fileRef} type="file" accept="image/jpeg,image/png,image/webp"
                           className="sr-only" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
                  </label>
                  <span className="text-[10px] text-faint">JPEG, PNG, WebP — 8 Mo au plus</span>
                </div>
                {file && (
                  <div className="grid gap-2 sm:grid-cols-[1fr_2fr_auto]">
                    <Input value={zone} onChange={(e) => setZone(e.target.value)} placeholder="Zone (facultatif)" className="h-9 text-xs" />
                    <Input value={caption} onChange={(e) => setCaption(e.target.value)} placeholder="Légende (facultatif)" className="h-9 text-xs" />
                    <Button type="button" size="sm" className="h-9" disabled={upload.isPending} onClick={doUpload}>
                      {upload.isPending ? <Spinner className="h-3.5 w-3.5" /> : <Plus className="h-3.5 w-3.5" />} Envoyer
                    </Button>
                  </div>
                )}
              </div>
            )}
          </div>

          <FormError message={error} />

          <div className="flex flex-wrap items-center gap-3 border-t border-line pt-3">
            {i.pv_pdf && (
              <span className="inline-flex items-center gap-1.5 text-xs">
                <FileText className="h-4 w-4 text-muted" />
                <SecureFileLink url={i.pv_pdf} label="Procès-verbal (PDF)" />
              </span>
            )}
            {!showSign && as === "manager" && !i.employee_signed_at && (
              <span className="text-xs text-muted">En attente de la validation du bénéficiaire (espace « Mon véhicule »).</span>
            )}
            {showSign && (
              <Button size="sm" variant="success" className="ml-auto" onClick={() => setConfirming(true)}>
                <CheckCircle2 className="h-4 w-4" />
                {as === "beneficiary" ? "Je valide cet état des lieux" : "Valider (gestionnaire)"}
              </Button>
            )}
          </div>
        </div>
      )}

      {confirming && (
        <Modal open title="Confirmer la validation" onClose={() => setConfirming(false)}>
          <p className="text-sm text-muted">
            {as === "beneficiary"
              ? "Vous confirmez que cet état des lieux correspond à l'état réel du véhicule. Une fois validé, il ne se modifie plus."
              : "Votre validation suit celle du bénéficiaire : l'état des lieux est figé, le procès-verbal est émis et l'attribution avance."}
          </p>
          <div className="flex justify-end gap-2 pt-4">
            <Button variant="secondary" onClick={() => setConfirming(false)}>Annuler</Button>
            <Button variant="success" disabled={sign.isPending} onClick={doSign}>
              {sign.isPending && <Spinner className="h-4 w-4" />} Valider
            </Button>
          </div>
        </Modal>
      )}
    </div>
  );
}

/** Comparaison automatique remise / restitution. */
export function ComparisonPanel({ comparison }: { comparison: Comparison | undefined }) {
  if (!comparison?.available) return null;
  return (
    <div className="rounded-xl border border-line bg-surface p-4">
      <p className="mb-2 text-sm font-semibold text-ink">Comparaison remise / restitution</p>
      <div className="mb-3 grid grid-cols-2 gap-3 text-sm">
        <div><p className="text-[11px] text-faint">Kilomètres parcourus</p><p className="font-medium text-ink">{km(comparison.km_driven)}</p></div>
        <div><p className="text-[11px] text-faint">Écart d&apos;énergie</p>
          <p className="font-medium text-ink">{(comparison.energy_delta_pct ?? 0) > 0 ? "+" : ""}{formatNumber(comparison.energy_delta_pct)} %</p></div>
      </div>
      {comparison.has_gaps ? (
        <ul className="space-y-1">
          {(comparison.gaps ?? []).map((g, idx) => (
            <li key={idx} className="flex items-start gap-2 text-xs text-rose-700 dark:text-rose-300">
              <X className="mt-0.5 h-3.5 w-3.5 shrink-0" /> {g.label}
            </li>
          ))}
        </ul>
      ) : <p className="text-xs text-emerald-700 dark:text-emerald-300">Aucun écart constaté.</p>}
    </div>
  );
}
