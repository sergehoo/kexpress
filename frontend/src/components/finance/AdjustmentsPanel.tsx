"use client";

import { useMemo, useState } from "react";
import { Check, Paperclip, Plus, ShieldAlert, Upload, X } from "lucide-react";

import { money } from "@/components/finance/RealCostPanel";
import { Modal } from "@/components/Modal";
import { SecureFileLink } from "@/components/SecureFileLink";
import { Button, Card, CardBody, EmptyState, Input, Label, Select, Spinner } from "@/components/ui";
import { apiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import {
  ATTACHMENT_KINDS, CATEGORY_LABEL, useAdjustments, useCreateAdjustment, useDecideAdjustment,
  useUploadAttachment, type Adjustment, type NewAdjustment,
} from "@/lib/financeF2";
import { useCostCenters, useSubsidiaries, useVehicles } from "@/lib/queries";
import { canFinance } from "@/lib/rbac";
import type { Me } from "@/lib/types";
import { cn, formatDate } from "@/lib/utils";

type Filter = "pending" | "approved" | "rejected" | "all";

const FILTERS: { key: Filter; label: string }[] = [
  { key: "pending", label: "À approuver" },
  { key: "approved", label: "Approuvés" },
  { key: "rejected", label: "Rejetés" },
  { key: "all", label: "Tous" },
];

const STATUS_TONE: Record<Adjustment["status"], string> = {
  pending: "bg-amber-500/10 text-amber-700",
  approved: "bg-emerald-500/10 text-emerald-700",
  rejected: "bg-rose-500/10 text-rose-700",
};

/** Miroir de `FinancialAdjustment.SOURCE_CHOICES`. */
const SOURCE_LABEL: Record<string, string> = {
  trip: "Course", mission: "Mission", vehicle: "Véhicule", expense: "Dépense", fuel_log: "Plein",
  electric_charge: "Recharge", maintenance: "Maintenance", insurance: "Assurance", other: "Autre",
};

/** Objets proposés à la saisie manuelle (les autres naissent des propositions de l'API). */
const NEW_SOURCES = ["trip", "mission", "vehicle", "expense", "other"] as const;
type NewSource = (typeof NEW_SOURCES)[number];

const SOURCE_ID_LABEL: Record<Exclude<NewSource, "other" | "vehicle">, string> = {
  trip: "Identifiant de la course", mission: "Identifiant de la mission", expense: "Identifiant de la dépense",
};

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const PERIOD = /^\d{4}-(0[1-9]|1[0-2])$/;
const ALLOWED_EXT = ["pdf", "jpg", "jpeg", "png", "webp", "heic", "heif"];
const MAX_SIZE = 10 * 1024 * 1024;

const TEXTAREA = "mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2 text-sm text-ink outline-none transition focus:border-brand-400 focus:ring-2 focus:ring-brand-100";

const AUTHOR_RULE = "Un ajustement est approuvé par une autre personne que son auteur : celui qui le passe ne le décide pas.";

function short(id: string) {
  return id.slice(0, 8);
}

function isAuthor(adj: Adjustment, me: Me | null) {
  return !!me && !!adj.created_by && String(adj.created_by) === String(me.id);
}

/** Ajustements financiers : corrections comptabilisées sur la période OUVERTE et rattachées à
 *  leur période d'origine — une période close ou un coût figé ne sont jamais réécrits. Chaque
 *  ajustement est motivé, justifié et décidé par une autre personne que son auteur. */
export function AdjustmentsPanel({ subsidiary }: { subsidiary: string }) {
  const { me } = useAuth();
  const canView = canFinance(me, "view_expense");
  const canDecide = canFinance(me, "validate_expense");
  const canCreate = canFinance(me, "create_expense");
  const [filter, setFilter] = useState<Filter>("pending");
  const [decision, setDecision] = useState<{ adjustment: Adjustment; decision: "approve" | "reject" } | null>(null);
  const [uploadFor, setUploadFor] = useState<Adjustment | null>(null);
  const [creating, setCreating] = useState(false);
  const [notice, setNotice] = useState("");

  const params = useMemo(() => {
    const p: Record<string, string> = { page_size: "100", ordering: "-created_at" };
    if (filter !== "all") p.status = filter;
    if (subsidiary) p.subsidiary = subsidiary;
    return p;
  }, [filter, subsidiary]);
  const list = useAdjustments(params, canView);
  const rows = list.data?.results ?? [];

  if (!canView) {
    return <Card><CardBody><EmptyState title="Accès réservé" hint="Les ajustements sont réservés aux profils habilités." /></CardBody></Card>;
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex flex-wrap rounded-lg border border-line bg-surface p-0.5">
          {FILTERS.map((f) => (
            <button key={f.key} type="button" onClick={() => setFilter(f.key)} aria-current={filter === f.key}
                    className={cn("rounded-md px-3 py-1.5 text-xs font-medium transition-colors",
                      filter === f.key ? "bg-brand-600 text-white" : "text-muted hover:bg-surface2")}>
              {f.label}
            </button>
          ))}
        </div>
        {canCreate && (
          <Button className="sm:ml-auto" onClick={() => { setNotice(""); setCreating(true); }}>
            <Plus className="h-4 w-4" /> Nouvel ajustement
          </Button>
        )}
      </div>
      <p className="text-xs text-muted">
        Un ajustement ne corrige que ce qui est figé (course clôturée, mission figée, mois clos) ; le reste se
        saisit en dépense ordinaire. {canDecide && AUTHOR_RULE}
      </p>

      {notice && (
        <div className="flex items-start gap-2 rounded-lg border border-emerald-500/30 bg-emerald-500/5 px-3 py-2 text-xs text-emerald-700">
          <span className="flex-1">{notice}</span>
          <button type="button" onClick={() => setNotice("")} aria-label="Fermer" className="text-faint hover:text-ink">
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      )}

      <Card>
        <CardBody className="p-0">
          {list.isLoading ? (
            <div className="flex justify-center py-12"><Spinner className="h-7 w-7" /></div>
          ) : list.isError ? (
            <p className="px-5 py-6 text-sm text-red-600">{apiError(list.error)}</p>
          ) : !rows.length ? (
            <EmptyState title={filter === "pending" ? "Aucun ajustement à approuver" : "Aucun ajustement"} />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-faint">
                    <th className="px-4 py-2.5 font-medium">Périodes</th>
                    <th className="px-4 py-2.5 font-medium">Objet</th>
                    <th className="px-4 py-2.5 font-medium">Filiale</th>
                    <th className="px-4 py-2.5 text-right font-medium">Montant</th>
                    <th className="px-4 py-2.5 font-medium">Motif</th>
                    <th className="px-4 py-2.5 font-medium">Statut</th>
                    <th className="px-4 py-2.5 font-medium">Auteur</th>
                    <th className="px-4 py-2.5 font-medium">Approbateur</th>
                    <th className="px-4 py-2.5 font-medium">Justificatifs</th>
                    {canDecide && <th className="px-4 py-2.5 font-medium">Décision</th>}
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {rows.map((a) => {
                    const negative = Number(a.amount) < 0;
                    const own = isAuthor(a, me);
                    return (
                      <tr key={a.id} className="align-top">
                        <td className="whitespace-nowrap px-4 py-2.5 text-xs">
                          <span className="font-medium text-ink">{a.original_period_label} → {a.posting_period_label}</span>
                          <span className="block text-[11px] text-faint">origine → comptabilisation</span>
                        </td>
                        <td className="px-4 py-2.5 text-xs">
                          <span className="block font-medium text-ink">{SOURCE_LABEL[a.source] ?? a.source}</span>
                          <ObjectRefs adjustment={a} />
                          {a.category && <span className="block text-faint">Nature : {CATEGORY_LABEL[a.category] ?? a.category}</span>}
                        </td>
                        <td className="px-4 py-2.5 text-xs text-muted">{a.subsidiary_name}</td>
                        <td className={cn("whitespace-nowrap px-4 py-2.5 text-right font-semibold", negative ? "text-rose-600" : "text-ink")}>
                          {money(a.amount, a.currency)}
                        </td>
                        <td className="max-w-xs px-4 py-2.5 text-xs text-muted">
                          <span className="block whitespace-pre-line text-ink">{a.reason}</span>
                          {a.decision_comment && (
                            <span className="mt-1 block text-faint">Décision : {a.decision_comment}</span>
                          )}
                        </td>
                        <td className="px-4 py-2.5">
                          <span className={cn("inline-flex whitespace-nowrap rounded-full px-2 py-0.5 text-[11px] font-medium", STATUS_TONE[a.status])}>
                            {a.status_display}
                          </span>
                        </td>
                        <td className="px-4 py-2.5 text-xs">
                          <span className="block text-ink">{a.author_name || "Système"}</span>
                          <span className="block text-faint">{formatDate(a.created_at, true)}</span>
                        </td>
                        <td className="px-4 py-2.5 text-xs">
                          <span className="block text-ink">{a.approved_by_name || "—"}</span>
                          {a.decided_at && <span className="block text-faint">{formatDate(a.decided_at, true)}</span>}
                        </td>
                        <td className="px-4 py-2.5">
                          <div className="flex flex-col items-start gap-1">
                            {a.attachments.map((f) => f.url ? (
                              <SecureFileLink key={f.id} url={f.url} label={`${f.kind_display} · ${f.name}`} />
                            ) : (
                              <span key={f.id} className="text-[11px] text-faint">{f.kind_display} (accès restreint)</span>
                            ))}
                            {!a.attachments.length && <span className="text-xs text-faint">Aucun</span>}
                            {canCreate && a.status === "pending" && (
                              <Button size="sm" variant="ghost" onClick={() => { setNotice(""); setUploadFor(a); }}>
                                <Paperclip className="h-3.5 w-3.5" /> Joindre
                              </Button>
                            )}
                          </div>
                        </td>
                        {canDecide && (
                          <td className="px-4 py-2.5">
                            {a.status === "pending" ? (
                              <div className="flex flex-col items-start gap-1.5">
                                <Button size="sm" variant="success" disabled={own}
                                        title={own ? AUTHOR_RULE : undefined}
                                        onClick={() => { setNotice(""); setDecision({ adjustment: a, decision: "approve" }); }}>
                                  <Check className="h-3.5 w-3.5" /> Approuver
                                </Button>
                                <Button size="sm" variant="secondary"
                                        onClick={() => { setNotice(""); setDecision({ adjustment: a, decision: "reject" }); }}>
                                  <X className="h-3.5 w-3.5" /> Rejeter
                                </Button>
                                {own && <span className="text-[11px] text-amber-700">Vous en êtes l&apos;auteur.</span>}
                              </div>
                            ) : (
                              <span className="text-xs text-faint">Décidé</span>
                            )}
                          </td>
                        )}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          {list.data && list.data.count > rows.length && (
            <p className="border-t border-line px-4 py-2 text-[11px] text-amber-700">
              Affichage des {rows.length} plus récents sur {list.data.count}.
            </p>
          )}
        </CardBody>
      </Card>

      {decision && (
        <DecisionDialog adjustment={decision.adjustment} decision={decision.decision} me={me}
                        onClose={() => setDecision(null)}
                        onDone={(updated) => {
                          setDecision(null);
                          setNotice(updated.status === "approved"
                            ? `Ajustement approuvé : comptabilisé sur ${updated.posting_period_label}, rattaché à ${updated.original_period_label}.`
                            : "Ajustement rejeté : il ne compte dans aucun coût.");
                        }} />
      )}
      {uploadFor && (
        <UploadDialog adjustment={uploadFor} onClose={() => setUploadFor(null)}
                      onDone={() => { setUploadFor(null); setNotice("Justificatif joint à l'ajustement."); }} />
      )}
      {creating && (
        <NewAdjustmentDialog me={me} subsidiary={subsidiary} known={rows} onClose={() => setCreating(false)}
                             onDone={() => {
                               setCreating(false);
                               setFilter("pending");
                               setNotice("Ajustement créé : il attend l'approbation d'une autre personne. Joignez-y son justificatif.");
                             }} />
      )}
    </div>
  );
}

/** Références de l'objet corrigé (identifiants abrégés : l'API ne sert que les clés). */
function ObjectRefs({ adjustment: a }: { adjustment: Adjustment }) {
  const refs: [string, string][] = [];
  if (a.trip) refs.push(["Course", a.trip]);
  if (a.mission) refs.push(["Mission", a.mission]);
  if (a.vehicle) refs.push(["Véhicule", a.vehicle]);
  if (a.expense) refs.push(["Dépense tardive", a.expense]);
  else if (a.source_id && !refs.some(([, id]) => id === a.source_id)) refs.push(["Réf.", a.source_id]);
  if (!refs.length) return null;
  return (
    <>
      {refs.map(([label, id]) => (
        <span key={`${label}-${id}`} className="block text-faint" title={id}>{label} #{short(id)}</span>
      ))}
    </>
  );
}

function Summary({ adjustment: a }: { adjustment: Adjustment }) {
  return (
    <div className="grid grid-cols-2 gap-2 rounded-lg bg-surface2 p-3 text-xs text-muted">
      <span>Période d&apos;origine : <b className="text-ink">{a.original_period_label}</b></span>
      <span>Comptabilisé sur : <b className="text-ink">{a.posting_period_label}</b></span>
      <span>Montant : <b className="text-ink">{money(a.amount, a.currency)}</b></span>
      <span>Objet : <b className="text-ink">{SOURCE_LABEL[a.source] ?? a.source}</b></span>
      <span className="col-span-2">Motif : <span className="text-ink">{a.reason}</span></span>
      <span className="col-span-2">Auteur : <span className="text-ink">{a.author_name || "Système"}</span> · {a.attachments.length} justificatif(s)</span>
    </div>
  );
}

function DecisionDialog({ adjustment: a, decision, me, onClose, onDone }: {
  adjustment: Adjustment;
  decision: "approve" | "reject";
  me: Me | null;
  onClose: () => void;
  onDone: (updated: Adjustment) => void;
}) {
  const decide = useDecideAdjustment();
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const own = decision === "approve" && isAuthor(a, me);

  function submit() {
    setError("");
    if (decision === "reject" && !text.trim()) { setError("Le motif du rejet est obligatoire."); return; }
    decide.mutate({ id: a.id, decision, text: text.trim() }, {
      onSuccess: onDone,
      onError: (e) => setError(apiError(e)),
    });
  }

  return (
    <Modal open title={decision === "approve" ? "Approuver l'ajustement" : "Rejeter l'ajustement"} onClose={onClose} className="max-w-lg">
      <div className="space-y-3 text-sm">
        <Summary adjustment={a} />
        {decision === "approve" ? (
          <>
            <p className={cn("flex items-start gap-1.5 rounded-lg border px-3 py-2 text-xs",
              own ? "border-amber-500/30 bg-amber-500/5 text-amber-800" : "border-line text-muted")}>
              <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              {own ? `Vous êtes l'auteur de cet ajustement. ${AUTHOR_RULE}` : AUTHOR_RULE}
            </p>
            {!a.attachments.length && (
              <p className="text-[11px] text-amber-700">Aucun justificatif n&apos;est joint à cet ajustement.</p>
            )}
            <p className="text-[11px] text-faint">
              Approuvé, il compte dans les coûts de {a.posting_period_label} (une mission est répartie entre ses
              courses) ; {a.original_period_label} n&apos;est pas modifiée.
            </p>
            <label className="block text-xs font-medium text-muted">Commentaire (facultatif)
              <textarea value={text} onChange={(e) => setText(e.target.value)} rows={2} className={TEXTAREA} />
            </label>
          </>
        ) : (
          <label className="block text-xs font-medium text-muted">Motif du rejet (obligatoire)
            <textarea value={text} onChange={(e) => setText(e.target.value)} rows={3} className={TEXTAREA}
                      placeholder="Ex. montant non justifié par la facture jointe" />
          </label>
        )}
        {error && <p className="text-xs text-red-600">{error}</p>}
      </div>
      <div className="flex justify-end gap-2 pt-3">
        <Button variant="secondary" onClick={onClose}>Fermer</Button>
        <Button variant={decision === "approve" ? "success" : "danger"} onClick={submit}
                disabled={decide.isPending || own}>
          {decision === "approve" ? "Approuver" : "Rejeter"}
        </Button>
      </div>
    </Modal>
  );
}

function UploadDialog({ adjustment: a, onClose, onDone }: {
  adjustment: Adjustment; onClose: () => void; onDone: () => void;
}) {
  const upload = useUploadAttachment("finance/adjustments");
  const [kind, setKind] = useState(ATTACHMENT_KINDS[0].value);
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState("");

  function submit() {
    setError("");
    if (!file) { setError("Choisissez un fichier."); return; }
    const ext = file.name.split(".").pop()?.toLowerCase() ?? "";
    if (!ALLOWED_EXT.includes(ext)) { setError("Format accepté : PDF, JPG, PNG, WEBP ou HEIC."); return; }
    if (file.size > MAX_SIZE) { setError("Fichier trop volumineux (10 Mo au maximum)."); return; }
    upload.mutate({ id: a.id, file, kind }, { onSuccess: onDone, onError: (e) => setError(apiError(e)) });
  }

  return (
    <Modal open title="Joindre un justificatif" onClose={onClose}>
      <div className="space-y-3 text-sm">
        <p className="text-xs text-muted">
          Ajustement {a.original_period_label} → {a.posting_period_label} · {money(a.amount, a.currency)}. Les
          justificatifs se joignent tant qu&apos;il est à approuver, puis sont figés.
        </p>
        <div>
          <Label htmlFor="adj-kind">Type de pièce</Label>
          <Select id="adj-kind" value={kind} onChange={(e) => setKind(e.target.value)}>
            {ATTACHMENT_KINDS.map((k) => <option key={k.value} value={k.value}>{k.label}</option>)}
          </Select>
        </div>
        <div>
          <Label htmlFor="adj-file">Fichier (PDF ou image, 10 Mo max.)</Label>
          <input id="adj-file" type="file"
                 accept=".pdf,.jpg,.jpeg,.png,.webp,.heic,.heif,application/pdf,image/*"
                 onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                 className="block w-full text-xs text-muted file:mr-3 file:rounded-lg file:border file:border-line file:bg-surface2 file:px-3 file:py-1.5 file:text-xs file:font-medium file:text-ink" />
        </div>
        {error && <p className="text-xs text-red-600">{error}</p>}
      </div>
      <div className="flex justify-end gap-2 pt-3">
        <Button variant="secondary" onClick={onClose}>Fermer</Button>
        <Button onClick={submit} disabled={upload.isPending || !file}>
          <Upload className="h-4 w-4" /> Joindre
        </Button>
      </div>
    </Modal>
  );
}

function VehicleSelect({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const vehicles = useVehicles({ page_size: "200" });
  return (
    <Select id="adj-vehicle" value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">{vehicles.isLoading ? "Chargement…" : "— Choisir un véhicule —"}</option>
      {(vehicles.data?.results ?? []).map((v) => (
        <option key={v.id} value={v.id}>{v.registration} · {v.brand} {v.model}</option>
      ))}
    </Select>
  );
}

function NewAdjustmentDialog({ me, subsidiary, known, onClose, onDone }: {
  me: Me | null;
  subsidiary: string;
  /** Ajustements déjà chargés : leurs filiales complètent la liste quand `/subsidiaries/` est vide. */
  known: Adjustment[];
  onClose: () => void;
  onDone: () => void;
}) {
  const create = useCreateAdjustment();
  // Filiale imposée pour un profil de filiale (l'API refuse les autres) ; choix libre pour le
  // périmètre entreprise et le financier groupe (D6).
  const ownSubsidiary = me?.subsidiary && !me.has_company_scope ? me.subsidiary : "";
  const subsidiaries = useSubsidiaries();
  const costCenters = useCostCenters();
  const [period, setPeriod] = useState("");
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");
  const [source, setSource] = useState<NewSource>("trip");
  const [objectId, setObjectId] = useState("");
  const [targetSub, setTargetSub] = useState(ownSubsidiary || subsidiary || me?.subsidiary || "");
  const [costCenter, setCostCenter] = useState("");
  const [category, setCategory] = useState("");
  const [error, setError] = useState("");

  const subOptions = useMemo(() => {
    const map = new Map<string, string>();
    (subsidiaries.data ?? []).forEach((s) => map.set(String(s.id), s.name));
    known.forEach((a) => map.set(String(a.subsidiary), a.subsidiary_name));
    if (me?.subsidiary) map.set(String(me.subsidiary), me.subsidiary_name ?? "Ma filiale");
    return [...map.entries()].sort((x, y) => x[1].localeCompare(y[1]));
  }, [subsidiaries.data, known, me]);
  const centers = (costCenters.data ?? []).filter((c) => c.active && (!targetSub || String(c.subsidiary) === String(targetSub)));

  function submit() {
    setError("");
    const value = amount.trim().replace(",", ".");
    const id = objectId.trim();
    if (!PERIOD.test(period.trim())) { setError("Période d'origine attendue au format AAAA-MM."); return; }
    if (!value || Number.isNaN(Number(value)) || Number(value) === 0) {
      setError("Montant obligatoire et non nul (négatif pour une réduction)."); return;
    }
    if (!reason.trim()) { setError("Le motif est obligatoire."); return; }
    if (!targetSub) { setError("Précisez la filiale imputée."); return; }
    if (id && source !== "vehicle" && !UUID.test(id)) {
      setError("Identifiant invalide : copiez l'identifiant complet de l'objet."); return;
    }
    const body: NewAdjustment = {
      original_period: period.trim(), amount: value, reason: reason.trim(), source,
      subsidiary: targetSub, category,
    };
    if (id) {
      body.source_id = id;
      if (source === "trip") body.trip = id;
      if (source === "mission") body.mission = id;
      if (source === "vehicle") body.vehicle = id;
    }
    if (costCenter) body.cost_center = costCenter;
    create.mutate(body, { onSuccess: onDone, onError: (e) => setError(apiError(e)) });
  }

  return (
    <Modal open title="Nouvel ajustement financier" onClose={onClose} className="max-w-lg">
      <div className="max-h-[75vh] space-y-3 overflow-y-auto pr-1 text-sm">
        <p className="rounded-lg border border-line px-3 py-2 text-xs text-muted">
          Comptabilisé sur la période ouverte, rattaché à sa période d&apos;origine. Refusé si ni la période ni
          l&apos;objet ne sont figés : saisissez alors une dépense ordinaire.
        </p>
        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <Label htmlFor="adj-period">Période d&apos;origine</Label>
            <Input id="adj-period" type="month" value={period} onChange={(e) => setPeriod(e.target.value)} placeholder="AAAA-MM" />
          </div>
          <div>
            <Label htmlFor="adj-amount">Montant (XOF)</Label>
            <Input id="adj-amount" type="number" step="0.01" value={amount} onChange={(e) => setAmount(e.target.value)}
                   placeholder="Ex. 25000 ou -5000" />
          </div>
        </div>
        <label className="block text-xs font-medium text-muted">Motif (obligatoire)
          <textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={3} className={TEXTAREA}
                    placeholder="Ex. facture de péage reçue après la clôture du mois" />
        </label>
        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <Label htmlFor="adj-source">Objet</Label>
            <Select id="adj-source" value={source} onChange={(e) => { setSource(e.target.value as NewSource); setObjectId(""); }}>
              {NEW_SOURCES.map((s) => <option key={s} value={s}>{SOURCE_LABEL[s]}</option>)}
            </Select>
          </div>
          {source === "vehicle" ? (
            <div>
              <Label htmlFor="adj-vehicle">Véhicule (facultatif)</Label>
              <VehicleSelect value={objectId} onChange={setObjectId} />
            </div>
          ) : source !== "other" ? (
            <div>
              <Label htmlFor="adj-object">{SOURCE_ID_LABEL[source]} (facultatif)</Label>
              <Input id="adj-object" value={objectId} onChange={(e) => setObjectId(e.target.value)}
                     placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" />
            </div>
          ) : null}
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <Label htmlFor="adj-sub">Filiale imputée</Label>
            {ownSubsidiary ? (
              <Input id="adj-sub" value={me?.subsidiary_name ?? "Ma filiale"} disabled />
            ) : (
              <Select id="adj-sub" value={targetSub} onChange={(e) => { setTargetSub(e.target.value); setCostCenter(""); }}>
                <option value="">— Choisir —</option>
                {subOptions.map(([id, name]) => <option key={id} value={id}>{name}</option>)}
              </Select>
            )}
          </div>
          <div>
            <Label htmlFor="adj-category">Nature (facultatif)</Label>
            <Select id="adj-category" value={category} onChange={(e) => setCategory(e.target.value)}>
              <option value="">—</option>
              {Object.entries(CATEGORY_LABEL).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
            </Select>
          </div>
        </div>
        <div>
          <Label htmlFor="adj-cc">Centre de coût (facultatif)</Label>
          <Select id="adj-cc" value={costCenter} onChange={(e) => setCostCenter(e.target.value)}>
            <option value="">—</option>
            {centers.map((c) => <option key={c.id} value={c.id}>{c.code} · {c.name}</option>)}
          </Select>
        </div>
        <p className="text-[11px] text-faint">Une autre personne l&apos;approuvera ; joignez son justificatif depuis la liste.</p>
        {error && <p className="text-xs text-red-600">{error}</p>}
      </div>
      <div className="flex justify-end gap-2 pt-3">
        <Button variant="secondary" onClick={onClose}>Fermer</Button>
        <Button onClick={submit} disabled={create.isPending}>Créer l&apos;ajustement</Button>
      </div>
    </Modal>
  );
}
