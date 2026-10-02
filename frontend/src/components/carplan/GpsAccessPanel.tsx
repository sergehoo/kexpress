"use client";

import { useState } from "react";
import Link from "next/link";
import { Ban, Check, MapPin, Plus, ShieldAlert, X } from "lucide-react";

import { Modal } from "@/components/Modal";
import { Button, Card, CardBody, EmptyState, Input, Label, Select, Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth";
import {
  carPlanError, GPS_MAX_HOURS, GPS_MIN_REASON, GPS_MOTIVE_LABEL, GPS_STATUS_LABEL, GPS_STATUS_TONE,
  useCarPlanVehicles, useGpsAccess, useGpsAccessAction, useRequestGpsAccess,
  type GpsAccessGrant, type GpsMotive,
} from "@/lib/carplan";

import { type Flash, FormError, formatDateTime, localInputToISO, nowLocalInput, Notice, Textarea, ToneBadge } from "./shared";

const MOTIVES = Object.entries(GPS_MOTIVE_LABEL) as [GpsMotive, string][];

/** Registre des accès exceptionnels à la position d'un véhicule attribué (`view_carplan_gps`).
 *  L'auditeur consulte le registre sans aucun geste. */
export function GpsAccessPanel({ onOpen }: { onOpen: (assignmentId: string) => void }) {
  const { me } = useAuth();
  const readOnly = me?.role === "auditor";
  const [status, setStatus] = useState("");
  const [motive, setMotive] = useState("");
  const [requesting, setRequesting] = useState(false);
  const [deciding, setDeciding] = useState<{ grant: GpsAccessGrant; action: "approve" | "reject" | "revoke" } | null>(null);
  const [flash, setFlash] = useState<Flash>(null);
  const params: Record<string, string> = {};
  if (status) params.status = status;
  if (motive) params.motive = motive;
  const { data, isLoading, error } = useGpsAccess(params);
  const now = Date.now();
  const mineActive = (data ?? []).filter((g) => g.grantee === me?.id && g.status === "approved"
    && new Date(g.starts_at).getTime() <= now && new Date(g.ends_at).getTime() > now);

  return (
    <div className="space-y-4">
      <Notice tone="warning">
        <p className="font-medium">Position d&apos;un véhicule attribué : vie privée du bénéficiaire.</p>
        <ul className="mt-1 list-disc space-y-0.5 pl-4">
          <li>Hors course mutualisée, la position d&apos;un véhicule de fonction ou de service n&apos;est visible que de son bénéficiaire :
            sur la carte, elle apparaît masquée (« position privée »).</li>
          <li>Une exception est <strong>nominative</strong> : seule la personne qui l&apos;a demandée voit la position, pendant la fenêtre
            accordée ({GPS_MAX_HOURS} h au plus).</li>
          <li>Elle est <strong>accordée par une autre personne</strong> que le demandeur, et révocable à tout moment.</li>
          <li>Le <strong>bénéficiaire est informé</strong> de l&apos;accord ; <strong>chaque consultation est journalisée</strong>.</li>
        </ul>
      </Notice>

      {mineActive.length > 0 && (
        <Notice tone="success">
          <span className="flex flex-wrap items-center gap-2">
            <MapPin className="h-4 w-4" />
            Exception en vigueur pour vous : {mineActive.map((g) => `${g.vehicle_registration} jusqu'au ${formatDateTime(g.ends_at)}`).join(" ; ")}.
            <Link href="/map" className="font-medium underline">Ouvrir la carte</Link>
          </span>
        </Notice>
      )}
      {flash && <Notice tone={flash.tone}>{flash.text}</Notice>}

      <div className="flex flex-wrap items-center gap-2">
        <Select value={status} onChange={(e) => setStatus(e.target.value)} className="sm:w-44" aria-label="Statut">
          <option value="">Tous statuts</option>
          {Object.entries(GPS_STATUS_LABEL).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </Select>
        <Select value={motive} onChange={(e) => setMotive(e.target.value)} className="sm:w-64" aria-label="Motif">
          <option value="">Tous motifs</option>
          {MOTIVES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </Select>
        {!readOnly && (
          <Button className="ml-auto" onClick={() => { setFlash(null); setRequesting(true); }}>
            <Plus className="h-4 w-4" /> Demander un accès
          </Button>
        )}
      </div>

      <Card>
        <CardBody className="p-0">
          {isLoading ? <div className="flex justify-center py-16"><Spinner className="h-7 w-7" /></div>
            : error ? <div className="p-4"><Notice tone="danger">{carPlanError(error)}</Notice></div>
            : !(data ?? []).length ? <EmptyState title="Aucun accès exceptionnel" />
            : (
              <ul className="divide-y divide-line">
                {(data ?? []).map((g) => {
                  const own = g.grantee === me?.id;
                  const expired = new Date(g.ends_at).getTime() <= now;
                  return (
                    <li key={g.id} className="flex flex-wrap items-start gap-3 px-5 py-3">
                      <div className="min-w-0 flex-1 space-y-0.5">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-sm font-semibold text-ink">{g.vehicle_registration}</span>
                          <ToneBadge tone={GPS_STATUS_TONE[g.status] ?? "slate"} label={g.status_display} />
                          {g.status === "approved" && expired && <ToneBadge tone="slate" label="Fenêtre expirée" />}
                          <span className="text-xs text-muted">{g.motive_display}</span>
                          {g.assignment && g.assignment_reference && (
                            <button className="text-xs font-medium text-brand-600 hover:underline" onClick={() => onOpen(g.assignment!)}>
                              {g.assignment_reference}
                            </button>
                          )}
                        </div>
                        <p className="whitespace-pre-line text-xs text-ink">{g.reason}</p>
                        <p className="text-[11px] text-muted">
                          Pour {g.grantee_name ?? "—"}{own ? " (vous)" : ""} · du {formatDateTime(g.starts_at)} au {formatDateTime(g.ends_at)}
                        </p>
                        <p className="text-[11px] text-faint">
                          Demandée le {formatDateTime(g.created_at)}
                          {g.decided_by_name ? ` · ${g.status === "rejected" ? "refusée" : "décidée"} par ${g.decided_by_name} le ${formatDateTime(g.decided_at)}` : ""}
                          {g.decision_note ? ` · « ${g.decision_note} »` : ""}
                          {g.revoked_at ? ` · révoquée le ${formatDateTime(g.revoked_at)}` : ""}
                        </p>
                        <p className="text-[11px] text-faint">
                          Consultations journalisées : {g.use_count}{g.last_used_at ? ` · dernière le ${formatDateTime(g.last_used_at)}` : ""}
                        </p>
                      </div>
                      {!readOnly && (
                        <div className="flex flex-wrap gap-2">
                          {g.status === "requested" && !own && (
                            <>
                              <Button size="sm" variant="success" onClick={() => setDeciding({ grant: g, action: "approve" })}><Check className="h-3.5 w-3.5" /> Accorder</Button>
                              <Button size="sm" variant="danger" onClick={() => setDeciding({ grant: g, action: "reject" })}><X className="h-3.5 w-3.5" /> Refuser</Button>
                            </>
                          )}
                          {(g.status === "requested" || (g.status === "approved" && !expired)) && (
                            <Button size="sm" variant="secondary" onClick={() => setDeciding({ grant: g, action: "revoke" })}><Ban className="h-3.5 w-3.5" /> Révoquer</Button>
                          )}
                        </div>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
        </CardBody>
      </Card>

      {requesting && (
        <RequestGpsDialog onClose={() => setRequesting(false)}
                          onDone={() => { setRequesting(false); setFlash({ tone: "success", text: "Demande enregistrée : elle doit être accordée par une autre personne habilitée." }); }} />
      )}
      {deciding && (
        <DecideGpsDialog grant={deciding.grant} action={deciding.action} onClose={() => setDeciding(null)}
                         onDone={(text) => { setDeciding(null); setFlash({ tone: "success", text }); }} />
      )}
    </div>
  );
}

function RequestGpsDialog({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const vehicles = useCarPlanVehicles();
  const request = useRequestGpsAccess();
  const held = (vehicles.data ?? []).filter((v) => v.holder);
  const [vehicle, setVehicle] = useState("");
  const [motive, setMotive] = useState<GpsMotive | "">("");
  const [reason, setReason] = useState("");
  const [startsAt, setStartsAt] = useState(nowLocalInput(1));
  const [endsAt, setEndsAt] = useState(nowLocalInput(24 * 60));
  const [error, setError] = useState("");

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (!vehicle || !motive) { setError("Véhicule et motif obligatoires."); return; }
    if (reason.trim().length < GPS_MIN_REASON) { setError(`Justification détaillée obligatoire (${GPS_MIN_REASON} caractères au moins).`); return; }
    const start = new Date(startsAt).getTime();
    const end = new Date(endsAt).getTime();
    if (!(end > start)) { setError("La fin doit suivre le début."); return; }
    if (end - start > GPS_MAX_HOURS * 3_600_000) { setError(`Une exception dure ${GPS_MAX_HOURS} heures au plus.`); return; }
    request.mutate({
      vehicle, motive, reason: reason.trim(), starts_at: localInputToISO(startsAt), ends_at: localInputToISO(endsAt),
    }, { onSuccess: onDone, onError: (err) => setError(carPlanError(err)) });
  }

  return (
    <Modal open title="Demander un accès exceptionnel à la position" onClose={onClose} className="max-w-lg">
      <form onSubmit={submit} className="space-y-3">
        <Notice tone="info">
          <span className="flex items-start gap-2"><ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
            L&apos;accès vous sera nominatif, après accord d&apos;une autre personne habilitée ; le bénéficiaire en sera informé et
            chaque consultation sera journalisée.</span>
        </Notice>
        <div><Label htmlFor="gps-vehicle">Véhicule attribué</Label>
          <Select id="gps-vehicle" value={vehicle} onChange={(e) => setVehicle(e.target.value)} required>
            <option value="">{vehicles.isLoading ? "Chargement…" : "Choisir un véhicule"}</option>
            {held.map((v) => <option key={v.id} value={v.id}>{v.registration} — {v.label} · {v.holder}</option>)}
          </Select></div>
        <div><Label htmlFor="gps-motive">Motif</Label>
          <Select id="gps-motive" value={motive} onChange={(e) => setMotive(e.target.value as GpsMotive)} required>
            <option value="">Choisir</option>
            {MOTIVES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </Select></div>
        <div><Label htmlFor="gps-reason">Justification détaillée</Label>
          <Textarea id="gps-reason" value={reason} required onChange={(e) => setReason(e.target.value)}
                    placeholder="Circonstances, référence de déclaration, autorité requérante…" />
          <p className={`mt-0.5 text-[10px] ${reason.trim().length < GPS_MIN_REASON ? "text-amber-700 dark:text-amber-300" : "text-faint"}`}>
            {reason.trim().length} / {GPS_MIN_REASON} caractères minimum</p></div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div><Label htmlFor="gps-start">Du</Label><Input id="gps-start" type="datetime-local" value={startsAt} required onChange={(e) => setStartsAt(e.target.value)} /></div>
          <div><Label htmlFor="gps-end">Au ({GPS_MAX_HOURS} h au plus)</Label><Input id="gps-end" type="datetime-local" value={endsAt} required onChange={(e) => setEndsAt(e.target.value)} /></div>
        </div>
        <FormError message={error} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose}>Annuler</Button>
          <Button type="submit" disabled={request.isPending}>{request.isPending && <Spinner className="h-4 w-4" />} Demander</Button>
        </div>
      </form>
    </Modal>
  );
}

function DecideGpsDialog({ grant, action, onClose, onDone }: {
  grant: GpsAccessGrant; action: "approve" | "reject" | "revoke"; onClose: () => void; onDone: (text: string) => void;
}) {
  const run = useGpsAccessAction();
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const title = action === "approve" ? "Accorder l'accès" : action === "reject" ? "Refuser l'accès" : "Révoquer l'accès";
  const success = action === "approve" ? "Accès accordé : le bénéficiaire est informé, chaque consultation sera journalisée."
    : action === "reject" ? "Demande refusée." : "Accès révoqué : la position redevient privée.";
  return (
    <Modal open title={`${title} — ${grant.vehicle_registration}`} onClose={onClose}>
      <div className="space-y-3">
        <p className="text-xs text-muted">{grant.motive_display} · pour {grant.grantee_name ?? "—"} · du {formatDateTime(grant.starts_at)} au {formatDateTime(grant.ends_at)}</p>
        <p className="whitespace-pre-line text-xs text-ink">{grant.reason}</p>
        {action !== "revoke" && (
          <div><Label htmlFor="gps-note">{action === "reject" ? "Motif du refus" : "Observation (facultative)"}</Label>
            <Textarea id="gps-note" value={note} required={action === "reject"} onChange={(e) => setNote(e.target.value)} /></div>
        )}
        <FormError message={error} />
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>Annuler</Button>
          <Button variant={action === "approve" ? "success" : "danger"} disabled={run.isPending} onClick={() => {
            setError("");
            if (action === "reject" && !note.trim()) { setError("Motif de refus obligatoire."); return; }
            run.mutate({ id: grant.id, action, note: note.trim() }, { onSuccess: () => onDone(success), onError: (err) => setError(carPlanError(err)) });
          }}>{run.isPending && <Spinner className="h-4 w-4" />} {title.split(" ")[0]}</Button>
        </div>
      </div>
    </Modal>
  );
}
