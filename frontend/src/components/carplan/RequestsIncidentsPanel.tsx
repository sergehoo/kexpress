"use client";

import { useState } from "react";
import { Check, CheckCheck, Lock, Wrench, X } from "lucide-react";

import { Modal } from "@/components/Modal";
import { SecureFileLink } from "@/components/SecureFileLink";
import { Button, Card, CardBody, CardHeader, CardTitle, EmptyState, Input, Label, Select, Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth";
import {
  canCarPlan, carPlanError, INCIDENT_KIND_LABEL, INCIDENT_STATUS_LABEL, INCIDENT_STATUS_TONE, REQUEST_KIND_LABEL,
  REQUEST_STATUS_LABEL, REQUEST_STATUS_TONE, useCarPlanIncidents, useCarPlanRequests, useCompleteRequest,
  useHandleRequest, useIncidentAction, type CarPlanIncident, type CarPlanRequest,
} from "@/lib/carplan";
import { useMaintenanceTypes } from "@/lib/queries";

import { type Flash, FormError, formatDateTime, formatDay, Notice, Textarea, ToneBadge } from "./shared";

/** Demandes des bénéficiaires et incidents déclarés : traitement par le gestionnaire. */
export function RequestsIncidentsPanel({ onOpen }: { onOpen: (assignmentId: string) => void }) {
  const { me } = useAuth();
  const canManage = canCarPlan(me, "manage_carplan_assignments");
  const [reqStatus, setReqStatus] = useState("open");
  const [reqKind, setReqKind] = useState("");
  const [incStatus, setIncStatus] = useState("open");
  const [handling, setHandling] = useState<{ request: CarPlanRequest; accept: boolean } | null>(null);
  const [completing, setCompleting] = useState<CarPlanRequest | null>(null);
  const [incident, setIncident] = useState<{ row: CarPlanIncident; action: "handle" | "close" } | null>(null);
  const [flash, setFlash] = useState<Flash>(null);

  const reqParams: Record<string, string> = {};
  if (reqStatus) reqParams.status = reqStatus;
  if (reqKind) reqParams.kind = reqKind;
  const requests = useCarPlanRequests(reqParams);
  const incParams: Record<string, string> = {};
  if (incStatus) incParams.status = incStatus;
  const incidents = useCarPlanIncidents(incParams);

  const done = (text: string) => setFlash({ tone: "success", text });

  return (
    <div className="space-y-5">
      {flash && <Notice tone={flash.tone}>{flash.text}</Notice>}

      <Card>
        <CardHeader className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle>Demandes des bénéficiaires</CardTitle>
          <div className="flex flex-wrap gap-2">
            <Select value={reqStatus} onChange={(e) => setReqStatus(e.target.value)} className="h-8 w-36 text-xs" aria-label="Statut des demandes">
              <option value="">Tous statuts</option>
              {Object.entries(REQUEST_STATUS_LABEL).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </Select>
            <Select value={reqKind} onChange={(e) => setReqKind(e.target.value)} className="h-8 w-40 text-xs" aria-label="Nature des demandes">
              <option value="">Toutes natures</option>
              {Object.entries(REQUEST_KIND_LABEL).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </Select>
          </div>
        </CardHeader>
        <CardBody className="p-0">
          {requests.isLoading ? <div className="flex justify-center py-10"><Spinner /></div>
            : requests.error ? <div className="p-4"><Notice tone="danger">{carPlanError(requests.error)}</Notice></div>
            : !(requests.data ?? []).length ? <EmptyState title="Aucune demande" />
            : (
              <ul className="divide-y divide-line">
                {(requests.data ?? []).map((r) => (
                  <li key={r.id} className="flex flex-wrap items-start gap-3 px-5 py-3">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-sm font-medium text-ink">{r.kind_display}</span>
                        <ToneBadge tone={REQUEST_STATUS_TONE[r.status] ?? "slate"} label={r.status_display} />
                        <button className="text-xs font-medium text-brand-600 hover:underline" onClick={() => onOpen(r.assignment)}>
                          {r.assignment_reference}
                        </button>
                      </div>
                      <p className="mt-0.5 whitespace-pre-line text-xs text-muted">{r.description}</p>
                      <p className="mt-0.5 text-[11px] text-faint">
                        {r.created_by_name ?? "—"} · {formatDateTime(r.created_at)}
                        {r.desired_date ? ` · souhaitée le ${formatDay(r.desired_date)}` : ""}
                        {r.handled_by_name ? ` · traitée par ${r.handled_by_name}` : ""}
                      </p>
                      {r.response && <p className="mt-1 text-xs text-ink"><span className="text-muted">Réponse :</span> {r.response}</p>}
                      {r.maintenance && <p className="mt-0.5 text-[11px] text-emerald-700 dark:text-emerald-300">Intervention planifiée dans le module Maintenance.</p>}
                    </div>
                    {canManage && r.status === "open" && (
                      <div className="flex gap-2">
                        <Button size="sm" variant="success" onClick={() => setHandling({ request: r, accept: true })}><Check className="h-3.5 w-3.5" /> Accepter</Button>
                        <Button size="sm" variant="danger" onClick={() => setHandling({ request: r, accept: false })}><X className="h-3.5 w-3.5" /> Refuser</Button>
                      </div>
                    )}
                    {canManage && r.status === "accepted" && (
                      <Button size="sm" variant="secondary" onClick={() => setCompleting(r)}><CheckCheck className="h-3.5 w-3.5" /> Marquer traitée</Button>
                    )}
                  </li>
                ))}
              </ul>
            )}
        </CardBody>
      </Card>

      <Card>
        <CardHeader className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle>Pannes, incidents et accidents</CardTitle>
          <Select value={incStatus} onChange={(e) => setIncStatus(e.target.value)} className="h-8 w-40 text-xs" aria-label="Statut des incidents">
            <option value="">Tous statuts</option>
            {Object.entries(INCIDENT_STATUS_LABEL).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </Select>
        </CardHeader>
        <CardBody className="p-0">
          {incidents.isLoading ? <div className="flex justify-center py-10"><Spinner /></div>
            : incidents.error ? <div className="p-4"><Notice tone="danger">{carPlanError(incidents.error)}</Notice></div>
            : !(incidents.data ?? []).length ? <EmptyState title="Aucun incident" />
            : (
              <ul className="divide-y divide-line">
                {(incidents.data ?? []).map((i) => (
                  <li key={i.id} className="flex flex-wrap items-start gap-3 px-5 py-3">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-sm font-medium text-ink">{i.kind_display ?? INCIDENT_KIND_LABEL[i.kind]}</span>
                        <ToneBadge tone={INCIDENT_STATUS_TONE[i.status] ?? "slate"} label={i.status_display} />
                        {!i.vehicle_drivable && <ToneBadge tone="red" label="Véhicule immobilisé" />}
                        <span className="text-xs text-muted">{i.vehicle_registration}</span>
                        <button className="text-xs font-medium text-brand-600 hover:underline" onClick={() => onOpen(i.assignment)}>
                          {i.assignment_reference}
                        </button>
                      </div>
                      <p className="mt-0.5 whitespace-pre-line text-xs text-muted">{i.description}</p>
                      <p className="mt-0.5 text-[11px] text-faint">
                        Survenu le {formatDateTime(i.occurred_at)}{i.location ? ` · ${i.location}` : ""}
                      </p>
                      <div className="mt-1 flex flex-wrap items-center gap-3">
                        {i.photo && <SecureFileLink url={i.photo} label="Voir la photo" />}
                        {i.maintenance && <span className="text-[11px] text-emerald-700 dark:text-emerald-300">Intervention ouverte dans le module Maintenance.</span>}
                      </div>
                    </div>
                    {canManage && i.status === "open" && (
                      <Button size="sm" onClick={() => setIncident({ row: i, action: "handle" })}><Wrench className="h-3.5 w-3.5" /> Prendre en charge</Button>
                    )}
                    {canManage && i.status !== "closed" && (
                      <Button size="sm" variant="secondary" onClick={() => setIncident({ row: i, action: "close" })}><Lock className="h-3.5 w-3.5" /> Clore</Button>
                    )}
                  </li>
                ))}
              </ul>
            )}
        </CardBody>
      </Card>

      {handling && (
        <HandleRequestDialog request={handling.request} accept={handling.accept} onClose={() => setHandling(null)}
                             onDone={(accepted) => { setHandling(null); done(accepted ? "Demande acceptée : le bénéficiaire est prévenu." : "Demande refusée : le bénéficiaire est prévenu."); }} />
      )}
      {completing && (
        <CompleteRequestDialog request={completing} onClose={() => setCompleting(null)}
                               onDone={() => { setCompleting(null); done("Demande marquée traitée."); }} />
      )}
      {incident && (
        <IncidentDialog incident={incident.row} action={incident.action} onClose={() => setIncident(null)}
                        onDone={() => { const a = incident.action; setIncident(null); done(a === "handle" ? "Incident pris en charge : le bénéficiaire est prévenu." : "Incident clos."); }} />
      )}
    </div>
  );
}

function HandleRequestDialog({ request, accept, onClose, onDone }: {
  request: CarPlanRequest; accept: boolean; onClose: () => void; onDone: (accepted: boolean) => void;
}) {
  const handle = useHandleRequest();
  const isMaintenance = accept && request.kind === "maintenance";
  const types = useMaintenanceTypes();
  const [response, setResponse] = useState("");
  const [maintenanceType, setMaintenanceType] = useState("");
  const [scheduled, setScheduled] = useState(request.desired_date ?? "");
  const [error, setError] = useState("");

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (!accept && !response.trim()) { setError("Motif de refus obligatoire."); return; }
    if (isMaintenance && !maintenanceType) { setError("Type d'intervention obligatoire pour planifier l'entretien."); return; }
    handle.mutate({
      id: request.id,
      body: {
        accept, response: response.trim() || undefined,
        ...(isMaintenance ? { maintenance_type: maintenanceType, scheduled_date: scheduled || undefined } : {}),
      },
    }, { onSuccess: () => onDone(accept), onError: (err) => setError(carPlanError(err)) });
  }

  return (
    <Modal open title={`${accept ? "Accepter" : "Refuser"} — ${request.kind_display}`} onClose={onClose} className="max-w-lg">
      <form onSubmit={submit} className="space-y-3">
        <p className="text-xs text-muted">{request.assignment_reference} · {request.description}</p>
        {accept && request.kind === "return" && (
          <Notice tone="info">Accepter fait passer l&apos;attribution en « restitution demandée » : réalisez ensuite l&apos;état des lieux de restitution.</Notice>
        )}
        {accept && request.kind === "renewal" && (
          <Notice tone="info">Accepter ne crée pas le renouvellement : utilisez « Renouveler » dans le détail de l&apos;attribution.</Notice>
        )}
        {accept && request.kind === "replacement" && (
          <Notice tone="info">Attribuez ensuite le véhicule de remplacement depuis le détail de l&apos;attribution (onglet « Remplacements »).</Notice>
        )}
        {isMaintenance && (
          <>
            <Notice tone="info">Une intervention planifiée est créée dans le module Maintenance (son coût y est suivi).</Notice>
            <div><Label htmlFor="hr-type">Type d&apos;intervention</Label>
              <Select id="hr-type" value={maintenanceType} onChange={(e) => setMaintenanceType(e.target.value)} required>
                <option value="">{types.isLoading ? "Chargement…" : "Choisir"}</option>
                {(types.data ?? []).map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
              </Select></div>
            <div><Label htmlFor="hr-date">Date planifiée</Label>
              <Input id="hr-date" type="date" value={scheduled} onChange={(e) => setScheduled(e.target.value)} /></div>
          </>
        )}
        <div><Label htmlFor="hr-response">{accept ? "Réponse au bénéficiaire (facultative)" : "Motif du refus"}</Label>
          <Textarea id="hr-response" value={response} required={!accept} onChange={(e) => setResponse(e.target.value)} /></div>
        <FormError message={error} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose}>Annuler</Button>
          <Button type="submit" variant={accept ? "success" : "danger"} disabled={handle.isPending}>
            {handle.isPending && <Spinner className="h-4 w-4" />} {accept ? "Accepter" : "Refuser"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function CompleteRequestDialog({ request, onClose, onDone }: { request: CarPlanRequest; onClose: () => void; onDone: () => void }) {
  const complete = useCompleteRequest();
  const [response, setResponse] = useState(request.response ?? "");
  const [error, setError] = useState("");
  return (
    <Modal open title={`Demande traitée — ${request.kind_display}`} onClose={onClose}>
      <div className="space-y-3">
        <div><Label htmlFor="cr-response">Réponse (facultative)</Label><Textarea id="cr-response" value={response} onChange={(e) => setResponse(e.target.value)} /></div>
        <FormError message={error} />
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>Annuler</Button>
          <Button disabled={complete.isPending} onClick={() => complete.mutate({ id: request.id, response: response.trim() || undefined }, {
            onSuccess: onDone, onError: (err) => setError(carPlanError(err)),
          })}>{complete.isPending && <Spinner className="h-4 w-4" />} Marquer traitée</Button>
        </div>
      </div>
    </Modal>
  );
}

function IncidentDialog({ incident, action, onClose, onDone }: {
  incident: CarPlanIncident; action: "handle" | "close"; onClose: () => void; onDone: () => void;
}) {
  const run = useIncidentAction();
  const types = useMaintenanceTypes();
  const [maintenanceType, setMaintenanceType] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  return (
    <Modal open title={`${action === "handle" ? "Prendre en charge" : "Clore"} — ${incident.kind_display}`} onClose={onClose} className="max-w-lg">
      <div className="space-y-3">
        <p className="text-xs text-muted">{incident.vehicle_registration} · {incident.description}</p>
        {action === "handle" && (
          <div>
            <Label htmlFor="ih-type">Ouvrir une intervention (facultatif)</Label>
            <Select id="ih-type" value={maintenanceType} onChange={(e) => setMaintenanceType(e.target.value)}>
              <option value="">Aucune intervention</option>
              {(types.data ?? []).map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
            </Select>
            <p className="mt-1 text-[11px] text-faint">
              Intervention {incident.vehicle_drivable ? "corrective" : "urgente (véhicule immobilisé)"} créée dans le module Maintenance.
            </p>
          </div>
        )}
        <div><Label htmlFor="ih-note">Observation (facultative)</Label><Textarea id="ih-note" value={note} onChange={(e) => setNote(e.target.value)} /></div>
        <FormError message={error} />
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>Annuler</Button>
          <Button disabled={run.isPending} onClick={() => run.mutate({
            id: incident.id, action,
            body: { note: note.trim() || undefined, ...(action === "handle" && maintenanceType ? { maintenance_type: maintenanceType } : {}) },
          }, { onSuccess: onDone, onError: (err) => setError(carPlanError(err)) })}>
            {run.isPending && <Spinner className="h-4 w-4" />} {action === "handle" ? "Prendre en charge" : "Clore"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
