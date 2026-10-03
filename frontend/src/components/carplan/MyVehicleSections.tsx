"use client";

import { useState } from "react";
import { Camera, Send } from "lucide-react";

import { SecureFileLink } from "@/components/SecureFileLink";
import { Button, Card, CardBody, CardHeader, CardTitle, EmptyState, Input, Label, Select, Spinner } from "@/components/ui";
import {
  carPlanError, INCIDENT_KIND_LABEL, INCIDENT_STATUS_TONE, REQUEST_KIND_LABEL, REQUEST_STATUS_TONE,
  useCorrectMyMileage, useCreateMyRequest, useDeclareMyIncident, useDeclareMyMileage, useMyHistory, useMyIncidents,
  useMyInspections, useMyMileage, useMyRequests, useMyTracking, type MileageReading, type MyVehicle,
} from "@/lib/carplan";
import { cn, formatNumber } from "@/lib/utils";

import { InspectionCard } from "./inspections";
import {
  AssignmentStatusBadge, FormError, formatDateTime, formatDay, km, localInputToISO, nowLocalInput, Notice, Textarea,
  todayISO, ToneBadge,
} from "./shared";

type Toast = (text: string, tone?: "success" | "danger") => void;

const IN_USE = ["active", "suspended", "returning"];

export function Section({ id, title, children, action }: {
  id: string; title: string; children: React.ReactNode; action?: React.ReactNode;
}) {
  return (
    <Card id={id} className="scroll-mt-20">
      <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-2">
        <CardTitle>{title}</CardTitle>
        {action}
      </CardHeader>
      <CardBody>{children}</CardBody>
    </Card>
  );
}

// --- Kilométrage ---------------------------------------------------------------------------------

export function MileageSection({ mine, onToast }: { mine: MyVehicle; onToast: Toast }) {
  const declare = useDeclareMyMileage();
  const readings = useMyMileage();
  const tracking = useMyTracking(!!mine.vehicle);
  const usage = mine.usage;
  const declaration = usage?.declaration ?? mine.conditions.mileage_declaration;
  const privateAllowed = mine.conditions.private_use_allowed;
  const status = tracking.data?.reading;
  // Plancher du compteur, comme l'API : le dernier relevé en vigueur (à défaut, le kilométrage initial).
  const lastReading = status?.last_reading ?? null;
  const fallback = [usage?.last_reading?.odometer, mine.start_mileage].filter((x): x is number => typeof x === "number");
  const last = lastReading ? lastReading.odometer : fallback.length ? Math.max(...fallback) : null;
  const [odometer, setOdometer] = useState("");
  const [date, setDate] = useState(todayISO());
  const [pro, setPro] = useState("");
  const [priv, setPriv] = useState("");
  const [error, setError] = useState("");
  const canDeclare = IN_USE.includes(mine.status) && !!mine.vehicle;

  const value = Number(odometer);
  const driven = odometer.trim() !== "" && Number.isFinite(value) && last !== null ? value - last : null;
  const split = declaration === "split";
  const correctable = (readings.data ?? []).find((r) => r.id === tracking.data?.correctable_reading) ?? null;

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (!odometer.trim() || !Number.isInteger(value) || value < 0) { setError("Indiquez le compteur en km (nombre entier)."); return; }
    if (last !== null && value < last) { setError(`Le compteur ne recule pas : dernier relevé ${formatNumber(last)} km.`); return; }
    const body: { odometer: number; reading_date?: string; professional_km?: number; private_km?: number } = {
      odometer: value, reading_date: date || undefined,
    };
    if (split) {
      if (privateAllowed) {
        const p = Number(pro);
        const q = Number(priv);
        if (pro.trim() === "" || priv.trim() === "" || !Number.isInteger(p) || !Number.isInteger(q) || p < 0 || q < 0) {
          setError("Ventilez les kilomètres professionnels et privés (nombres entiers).");
          return;
        }
        if (driven !== null && p + q !== driven) {
          setError(`Ventilation incohérente : ${p} + ${q} km ≠ ${driven} km parcourus depuis le dernier relevé.`);
          return;
        }
        body.professional_km = p;
        body.private_km = q;
      } else {
        // Usage privé non autorisé : tout le trajet est professionnel.
        body.professional_km = Math.max(0, driven ?? value);
        body.private_km = 0;
      }
    }
    declare.mutate(body, {
      onSuccess: (r) => {
        setOdometer(""); setPro(""); setPriv(""); setDate(todayISO());
        onToast(r.anomaly ? "Relevé enregistré — valeur inhabituelle signalée à votre gestionnaire." : "Relevé kilométrique enregistré.");
      },
      onError: (err) => setError(carPlanError(err)),
    });
  }

  return (
    <Section id="kilometrage" title="Relevé kilométrique"
             action={status?.required ? <span className="text-[11px] text-muted">Attendu le {formatDay(status.next_due)}</span> : undefined}>
      {!canDeclare ? (
        <p className="text-sm text-muted">La déclaration s&apos;ouvre une fois le véhicule remis (état des lieux validé).</p>
      ) : (
        <form onSubmit={submit} className="space-y-3">
          <p className="text-xs text-muted">
            {declaration === "none" ? "Votre politique n'exige pas de relevé ; vous pouvez néanmoins déclarer votre compteur."
              : declaration === "split" ? `Relevé attendu tous les ${status?.frequency_days ?? 7} jours, kilomètres professionnels et privés ventilés.`
              : `Relevé attendu tous les ${status?.frequency_days ?? 7} jours.`}
            {last !== null && <> Dernier relevé : <span className="font-medium text-ink">{km(last)}</span>
              {lastReading ? ` (${formatDateTime(lastReading.recorded_at)})` : usage?.last_reading?.date ? ` (${formatDay(usage.last_reading.date)})` : ""}.</>}
          </p>
          <div className="grid gap-3 sm:grid-cols-[2fr_1fr]">
            <div><Label htmlFor="my-odo">Compteur actuel (km)</Label>
              <Input id="my-odo" type="number" inputMode="numeric" min={last ?? 0} required value={odometer} autoComplete="off"
                     placeholder={last !== null ? String(last) : undefined}
                     onChange={(e) => setOdometer(e.target.value)} className="h-12 text-lg font-semibold tabular-nums sm:h-10 sm:text-sm" /></div>
            <div><Label htmlFor="my-date">Date du relevé</Label>
              <Input id="my-date" type="date" max={todayISO()} value={date} onChange={(e) => setDate(e.target.value)} className="text-base sm:text-sm" /></div>
          </div>
          {driven !== null && driven >= 0 && (
            <p className="text-xs text-muted">Kilomètres parcourus depuis le dernier relevé : <span className="font-semibold text-ink">{km(driven)}</span></p>
          )}
          {split && privateAllowed && (
            <div className="grid gap-3 sm:grid-cols-2">
              <div><Label htmlFor="my-pro">Dont professionnels (km)</Label>
                <Input id="my-pro" type="number" inputMode="numeric" min={0} value={pro} required
                       onChange={(e) => { setPro(e.target.value); if (driven !== null && e.target.value !== "") setPriv(String(Math.max(0, driven - Number(e.target.value)))); }}
                       className="text-base sm:text-sm" /></div>
              <div><Label htmlFor="my-priv">Dont privés (km)</Label>
                <Input id="my-priv" type="number" inputMode="numeric" min={0} value={priv} required
                       onChange={(e) => setPriv(e.target.value)} className="text-base sm:text-sm" /></div>
            </div>
          )}
          {split && !privateAllowed && (
            <p className="text-[11px] text-faint">Usage privé non autorisé par votre politique : les kilomètres sont déclarés professionnels.</p>
          )}
          <FormError message={error} />
          <Button type="submit" className="h-12 w-full text-base sm:h-auto sm:w-auto sm:text-sm" disabled={declare.isPending}>
            {declare.isPending ? <Spinner className="h-4 w-4" /> : <Send className="h-4 w-4" />} Déclarer
          </Button>
        </form>
      )}

      {canDeclare && correctable && <CorrectionForm reading={correctable} onToast={onToast} />}

      <div className="mt-4 border-t border-line pt-3">
        <p className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-faint">Historique de mes relevés</p>
        {readings.isLoading ? <Spinner /> : !(readings.data ?? []).length ? <p className="text-xs text-muted">Aucun relevé.</p> : (
          <ul className="divide-y divide-line text-sm">
            {[...(readings.data ?? [])].reverse().slice(0, 20).map((r) => (
              <li key={r.id} className={cn("flex flex-wrap items-center justify-between gap-x-2 gap-y-0.5 py-2", r.superseded && "opacity-60")}>
                <span className="text-muted">{formatDateTime(r.recorded_at)}</span>
                <span className={cn("font-medium text-ink tabular-nums", r.superseded && "line-through")}>{km(r.odometer)}</span>
                <span className="w-full text-[11px] text-faint">
                  {r.source_display}{r.by_manager && r.source !== "handover" && r.source !== "return" ? " · par votre gestionnaire" : ""}
                  {r.professional_km != null ? ` · pro ${formatNumber(r.professional_km)} km` : ""}
                  {r.private_km != null ? ` · privé ${formatNumber(r.private_km)} km` : ""}
                  {r.previous_odometer != null ? ` · ancien compteur ${km(r.previous_odometer)}` : ""}
                </span>
                {r.superseded && <span className="w-full text-[11px] text-amber-700 dark:text-amber-300">Relevé corrigé (conservé pour la trace)</span>}
                {r.corrects && <span className="w-full text-[11px] text-sky-700 dark:text-sky-300">Correction{r.reason ? ` : ${r.reason}` : ""}</span>}
                {r.anomaly && !r.superseded && <span className="w-full text-[11px] text-amber-700 dark:text-amber-300">{r.anomaly}</span>}
              </li>
            ))}
          </ul>
        )}
      </div>
    </Section>
  );
}

/** Correction de SA dernière déclaration (48 h après la déclaration, deux corrections au plus) : un
 *  nouveau relevé remplace l'erroné, qui reste tracé. */
function CorrectionForm({ reading, onToast }: { reading: MileageReading; onToast: Toast }) {
  const correct = useCorrectMyMileage();
  const [open, setOpen] = useState(false);
  const [odometer, setOdometer] = useState("");
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    const n = Number(odometer);
    if (!odometer.trim() || !Number.isInteger(n) || n < 0) { setError("Indiquez le bon compteur en km (nombre entier)."); return; }
    correct.mutate({ readingId: reading.id, odometer: n, reason: reason.trim() || undefined }, {
      onSuccess: () => { setOpen(false); setOdometer(""); setReason(""); onToast("Relevé corrigé : l'ancienne valeur reste visible dans l'historique."); },
      onError: (err) => setError(carPlanError(err)),
    });
  }

  if (!open) {
    return (
      <button type="button" onClick={() => setOpen(true)} className="mt-3 text-xs font-medium text-brand-600 hover:underline">
        Erreur de saisie ? Corriger mon dernier relevé ({km(reading.odometer)})
      </button>
    );
  }
  return (
    <form onSubmit={submit} className="mt-3 space-y-2 rounded-xl border border-line bg-surface2 p-3">
      <p className="text-xs text-muted">Correction possible dans les 48 h qui suivent votre déclaration, deux fois au plus ; au-delà, demandez-la à votre gestionnaire.</p>
      <div className="grid gap-2 sm:grid-cols-2">
        <div><Label htmlFor="fix-odo">Bon compteur (km)</Label>
          <Input id="fix-odo" type="number" inputMode="numeric" min={0} value={odometer} required
                 onChange={(e) => setOdometer(e.target.value)} className="text-base sm:text-sm" /></div>
        <div><Label htmlFor="fix-reason">Motif (facultatif)</Label>
          <Input id="fix-reason" value={reason} maxLength={500} placeholder="Ex. chiffre en trop"
                 onChange={(e) => setReason(e.target.value)} className="text-base sm:text-sm" /></div>
      </div>
      <FormError message={error} />
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={correct.isPending}>{correct.isPending && <Spinner className="h-4 w-4" />} Corriger</Button>
        <Button type="button" size="sm" variant="secondary" onClick={() => setOpen(false)}>Annuler</Button>
      </div>
    </form>
  );
}

// --- Demandes ------------------------------------------------------------------------------------

export function RequestsSection({ onToast }: { onToast: Toast }) {
  const requests = useMyRequests();
  const create = useCreateMyRequest();
  const [kind, setKind] = useState("");
  const [description, setDescription] = useState("");
  const [desired, setDesired] = useState("");
  const [error, setError] = useState("");

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (!kind) { setError("Choisissez la nature de la demande."); return; }
    if (!description.trim()) { setError("Décrivez votre demande."); return; }
    create.mutate({ kind, description: description.trim(), desired_date: desired || undefined }, {
      onSuccess: () => { setKind(""); setDescription(""); setDesired(""); onToast("Demande envoyée à votre gestionnaire."); },
      onError: (err) => setError(carPlanError(err)),
    });
  }

  return (
    <Section id="demandes" title="Mes demandes">
      <form onSubmit={submit} className="space-y-3">
        <div className="grid gap-3 sm:grid-cols-2">
          <div><Label htmlFor="rq-kind">Nature</Label>
            <Select id="rq-kind" value={kind} onChange={(e) => setKind(e.target.value)} required className="text-base sm:text-sm">
              <option value="">Choisir</option>
              {Object.entries(REQUEST_KIND_LABEL).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </Select></div>
          <div><Label htmlFor="rq-date">Date souhaitée (facultative)</Label>
            <Input id="rq-date" type="date" min={todayISO()} value={desired} onChange={(e) => setDesired(e.target.value)} className="text-base sm:text-sm" /></div>
        </div>
        <div><Label htmlFor="rq-desc">Description</Label>
          <Textarea id="rq-desc" value={description} required onChange={(e) => setDescription(e.target.value)}
                    placeholder="Ex. voyant d'entretien allumé, révision des 30 000 km…" className="text-base sm:text-sm" /></div>
        <FormError message={error} />
        <Button type="submit" className="w-full sm:w-auto" disabled={create.isPending}>
          {create.isPending ? <Spinner className="h-4 w-4" /> : <Send className="h-4 w-4" />} Envoyer la demande
        </Button>
      </form>

      <div className="mt-4 border-t border-line pt-3">
        {requests.isLoading ? <Spinner /> : !(requests.data ?? []).length ? <p className="text-xs text-muted">Aucune demande.</p> : (
          <ul className="space-y-2">
            {(requests.data ?? []).map((r) => (
              <li key={r.id} className="rounded-lg border border-line px-3 py-2">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-medium text-ink">{r.kind_display}</span>
                  <ToneBadge tone={REQUEST_STATUS_TONE[r.status] ?? "slate"} label={r.status_display} />
                  <span className="ml-auto text-[11px] text-faint">{formatDateTime(r.created_at)}</span>
                </div>
                <p className="mt-0.5 whitespace-pre-line text-xs text-muted">{r.description}</p>
                {r.desired_date && <p className="text-[11px] text-faint">Souhaitée le {formatDay(r.desired_date)}</p>}
                {r.response && <p className="mt-1 text-xs text-ink"><span className="text-muted">Réponse{r.handled_by_name ? ` de ${r.handled_by_name}` : ""} :</span> {r.response}</p>}
              </li>
            ))}
          </ul>
        )}
      </div>
    </Section>
  );
}

// --- Incidents -----------------------------------------------------------------------------------

export function IncidentSection({ mine, onToast }: { mine: MyVehicle; onToast: Toast }) {
  const incidents = useMyIncidents();
  const declare = useDeclareMyIncident();
  const [kind, setKind] = useState("");
  const [occurred, setOccurred] = useState(nowLocalInput());
  const [location, setLocation] = useState("");
  const [description, setDescription] = useState("");
  const [drivable, setDrivable] = useState(true);
  const [photo, setPhoto] = useState<File | null>(null);
  const [error, setError] = useState("");
  const [inputKey, setInputKey] = useState(0);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (!kind) { setError("Choisissez la nature de l'incident."); return; }
    if (!description.trim()) { setError("Décrivez l'incident."); return; }
    if (!occurred) { setError("Indiquez la date et l'heure."); return; }
    if (photo && photo.size > 8 * 1024 * 1024) { setError("Photo trop lourde (8 Mo au plus)."); return; }
    declare.mutate({
      kind, occurred_at: localInputToISO(occurred), description: description.trim(), location: location.trim() || undefined,
      vehicle_drivable: drivable, photo,
    }, {
      onSuccess: () => {
        setKind(""); setDescription(""); setLocation(""); setPhoto(null); setDrivable(true); setOccurred(nowLocalInput());
        setInputKey((k) => k + 1);
        onToast("Déclaration envoyée : votre gestionnaire est prévenu.");
      },
      onError: (err) => setError(carPlanError(err)),
    });
  }

  if (!mine.vehicle) return null;

  return (
    <Section id="incident" title="Déclarer une panne, un incident ou un accident">
      <form onSubmit={submit} className="space-y-3">
        <Notice tone="warning">En cas d&apos;accident avec blessés, appelez d&apos;abord les secours. Cette déclaration prévient votre gestionnaire.</Notice>
        <div className="grid gap-3 sm:grid-cols-2">
          <div><Label htmlFor="inc-kind">Nature</Label>
            <Select id="inc-kind" value={kind} onChange={(e) => setKind(e.target.value)} required className="text-base sm:text-sm">
              <option value="">Choisir</option>
              {Object.entries(INCIDENT_KIND_LABEL).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </Select></div>
          <div><Label htmlFor="inc-at">Date et heure</Label>
            <Input id="inc-at" type="datetime-local" max={nowLocalInput(5)} value={occurred} required
                   onChange={(e) => setOccurred(e.target.value)} className="text-base sm:text-sm" /></div>
        </div>
        <div><Label htmlFor="inc-loc">Lieu (facultatif)</Label>
          <Input id="inc-loc" value={location} onChange={(e) => setLocation(e.target.value)} placeholder="Adresse, axe, ville…" className="text-base sm:text-sm" /></div>
        <div><Label htmlFor="inc-desc">Description</Label>
          <Textarea id="inc-desc" value={description} required onChange={(e) => setDescription(e.target.value)} className="text-base sm:text-sm" /></div>
        <fieldset>
          <legend className="mb-1 text-xs font-medium text-muted">Le véhicule peut-il rouler ?</legend>
          <div className="grid grid-cols-2 gap-2">
            {[{ v: true, l: "Oui, il roule" }, { v: false, l: "Non, immobilisé" }].map((o) => (
              <button key={String(o.v)} type="button" onClick={() => setDrivable(o.v)} aria-pressed={drivable === o.v}
                      className={cn("h-11 rounded-lg border text-sm font-medium transition-colors",
                        drivable === o.v
                          ? o.v ? "border-emerald-500 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300" : "border-rose-500 bg-rose-500/10 text-rose-700 dark:text-rose-300"
                          : "border-line text-muted hover:bg-surface2")}>
                {o.l}
              </button>
            ))}
          </div>
        </fieldset>
        <div>
          <label className="inline-flex cursor-pointer items-center gap-2 rounded-lg border border-line bg-surface px-3 py-2.5 text-sm font-medium text-ink hover:bg-surface2">
            <Camera className="h-4 w-4" /> {photo ? photo.name : "Joindre une photo (facultatif)"}
            <input key={inputKey} type="file" accept="image/jpeg,image/png,image/webp" className="sr-only"
                   onChange={(e) => setPhoto(e.target.files?.[0] ?? null)} />
          </label>
          {photo && <button type="button" className="ml-2 text-xs text-muted underline" onClick={() => { setPhoto(null); setInputKey((k) => k + 1); }}>Retirer</button>}
        </div>
        <FormError message={error} />
        <Button type="submit" variant="danger" className="w-full sm:w-auto" disabled={declare.isPending}>
          {declare.isPending ? <Spinner className="h-4 w-4" /> : <Send className="h-4 w-4" />} Envoyer la déclaration
        </Button>
      </form>

      <div className="mt-4 border-t border-line pt-3">
        <p className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-faint">Mes déclarations</p>
        {incidents.isLoading ? <Spinner /> : !(incidents.data ?? []).length ? <p className="text-xs text-muted">Aucune déclaration.</p> : (
          <ul className="space-y-2">
            {(incidents.data ?? []).map((i) => (
              <li key={i.id} className="rounded-lg border border-line px-3 py-2">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-medium text-ink">{i.kind_display}</span>
                  <ToneBadge tone={INCIDENT_STATUS_TONE[i.status] ?? "slate"} label={i.status_display} />
                  {!i.vehicle_drivable && <ToneBadge tone="red" label="Immobilisé" />}
                  <span className="ml-auto text-[11px] text-faint">{formatDateTime(i.occurred_at)}</span>
                </div>
                <p className="mt-0.5 whitespace-pre-line text-xs text-muted">{i.description}</p>
                {i.location && <p className="text-[11px] text-faint">{i.location}</p>}
                {i.photo && <div className="mt-1"><SecureFileLink url={i.photo} label="Voir la photo" /></div>}
              </li>
            ))}
          </ul>
        )}
      </div>
    </Section>
  );
}

// --- États des lieux & historique ------------------------------------------------------------------

export function InspectionsHistorySection({ pendingId, onToast }: { pendingId?: string | null; onToast: Toast }) {
  const inspections = useMyInspections();
  const list = (inspections.data ?? []).filter((i) => i.id !== pendingId);
  if (inspections.isLoading || list.length === 0) return null;
  return (
    <Section id="etats-des-lieux" title="Mes états des lieux">
      <div className="space-y-2">
        {list.map((i) => (
          <InspectionCard key={i.id} inspection={i} as="beneficiary" canSign={!i.employee_signed_at} canAddPhoto={!i.is_signed} onFlash={onToast} />
        ))}
      </div>
    </Section>
  );
}

export function HistorySection({ hideWhenEmpty = false }: { hideWhenEmpty?: boolean }) {
  const history = useMyHistory();
  if (hideWhenEmpty && !history.isLoading && !(history.data ?? []).length) return null;
  return (
    <Section id="historique" title="Historique de mes attributions">
      {history.isLoading ? <Spinner /> : !(history.data ?? []).length ? <EmptyState title="Aucune attribution" /> : (
        <ul className="space-y-2">
          {(history.data ?? []).map((h) => (
            <li key={h.id} className="rounded-lg border border-line px-3 py-2">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-medium text-ink">{h.vehicle ? `${h.vehicle.registration} — ${h.vehicle.brand} ${h.vehicle.model}` : "Véhicule non attribué"}</span>
                <AssignmentStatusBadge status={h.status} label={h.status_display} />
              </div>
              <p className="text-xs text-muted">
                {h.reference} · {h.assignment_type_display} · du {formatDay(h.start_date)}{h.planned_end_date ? ` au ${formatDay(h.planned_end_date)}` : ""}
              </p>
              <p className="text-[11px] text-faint">Politique {h.conditions.policy} (version {h.conditions.version})</p>
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}
