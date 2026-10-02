"use client";

import { useState } from "react";
import {
  ArrowRightLeft, Ban, CalendarPlus, CarFront, CheckCircle2, ClipboardCheck, Lock, PauseCircle, PlayCircle,
  RefreshCw, Share2, Undo2, XCircle,
} from "lucide-react";

import { Modal } from "@/components/Modal";
import { Button, Input, Label, Select, Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth";
import {
  canCarPlan, carPlanError, MODE_FOR_TYPE, MODE_LABEL, useAssignmentAction, useCarPlanVehicles,
  type Assignment, type AssignmentAction, type AssignmentStatus, type CarPlanPerm,
} from "@/lib/carplan";

import { FormError, localInputToISO, nowLocalInput, Notice, Textarea, todayISO } from "./shared";

type FieldName = "note" | "reason" | "vehicle" | "new_end" | "new_end_optional" | "on_date" | "starts_at" | "ends_at";

interface ActionDef {
  action: AssignmentAction;
  label: string;
  title: string;
  perm: CarPlanPerm;
  from: AssignmentStatus[];
  fields: FieldName[];
  icon: React.ElementType;
  variant?: "primary" | "secondary" | "danger" | "success";
  hint?: string;
  submit?: string;
}

const ACTIONS: ActionDef[] = [
  { action: "validate", label: "Valider", title: "Valider la demande", perm: "approve_carplan_assignments",
    from: ["requested"], fields: ["note"], icon: CheckCircle2, variant: "success",
    hint: "La validation revient à une autre personne que l'auteur de la demande. Elle réserve la période pour le bénéficiaire." },
  { action: "reject", label: "Refuser", title: "Refuser la demande", perm: "approve_carplan_assignments",
    from: ["requested"], fields: ["reason"], icon: XCircle, variant: "danger" },
  { action: "allocate", label: "Attribuer un véhicule", title: "Attribuer un véhicule", perm: "manage_carplan_assignments",
    from: ["validated"], fields: ["vehicle"], icon: CarFront, variant: "primary",
    hint: "Le véhicule est tenu pour toute la période (exclusivité garantie) ; reste l'état des lieux de remise." },
  { action: "extend", label: "Prolonger", title: "Prolonger l'attribution", perm: "approve_carplan_assignments",
    from: ["validated", "allocated", "active", "suspended"], fields: ["new_end", "reason"], icon: CalendarPlus,
    hint: "Dans la durée maximale de la politique ; au-delà, préparez un renouvellement." },
  { action: "change-vehicle", label: "Changer de véhicule", title: "Changer de véhicule", perm: "manage_carplan_assignments",
    from: ["allocated", "active", "suspended"], fields: ["vehicle", "on_date", "reason"], icon: ArrowRightLeft,
    hint: "L'attribution repasse « véhicule attribué » jusqu'à l'état des lieux de remise du nouveau véhicule." },
  { action: "suspend", label: "Suspendre", title: "Suspendre l'attribution", perm: "approve_carplan_assignments",
    from: ["active"], fields: ["reason"], icon: PauseCircle },
  { action: "resume", label: "Reprendre", title: "Reprendre l'attribution", perm: "approve_carplan_assignments",
    from: ["suspended"], fields: ["note"], icon: PlayCircle, variant: "success" },
  { action: "request-return", label: "Demander la restitution", title: "Demander la restitution", perm: "manage_carplan_assignments",
    from: ["active", "suspended"], fields: ["note"], icon: Undo2,
    hint: "La restitution s'achève par l'état des lieux de restitution validé par les deux parties." },
  { action: "renew", label: "Renouveler", title: "Renouveler l'attribution", perm: "manage_carplan_assignments",
    from: ["active", "suspended"], fields: ["new_end_optional", "note"], icon: RefreshCw,
    hint: "Crée une NOUVELLE attribution, à valider, qui prend la suite au lendemain de la fin prévue, sous la politique alors applicable.",
    submit: "Créer le renouvellement" },
  { action: "release", label: "Mettre à disposition", title: "Mettre à disposition du dispatching", perm: "approve_carplan_assignments",
    from: ["allocated", "active", "suspended", "returning"], fields: ["starts_at", "ends_at", "reason"], icon: Share2,
    hint: "Mise à disposition TEMPORAIRE (31 jours au plus) : le dispatching pourra affecter ce véhicule sur ce créneau seulement." },
  { action: "close", label: "Clôturer", title: "Clôturer l'attribution", perm: "manage_carplan_assignments",
    from: ["returned"], fields: ["note"], icon: Lock, variant: "primary" },
  { action: "cancel", label: "Annuler", title: "Annuler l'attribution", perm: "manage_carplan_assignments",
    from: ["requested", "validated", "allocated"], fields: ["reason"], icon: Ban, variant: "danger",
    hint: "Le véhicule éventuellement tenu est libéré. L'annulation est historisée." },
];

const FIELD_LABEL: Record<FieldName, string> = {
  note: "Observation (facultative)",
  reason: "Motif",
  vehicle: "Véhicule",
  new_end: "Nouvelle fin prévue",
  new_end_optional: "Fin prévue du renouvellement (facultative selon la politique)",
  on_date: "À compter du",
  starts_at: "Du",
  ends_at: "Au",
};

/** Boutons du circuit selon le STATUT de l'attribution ET les permissions du profil (l'API
 *  reste la barrière : un geste refusé affiche son motif). */
export function AssignmentActions({ assignment, onDone, onInspection }: {
  assignment: Assignment;
  onDone: (message: string, created?: { id: string; reference?: string }) => void;
  onInspection: (kind: "handover" | "return") => void;
}) {
  const { me } = useAuth();
  const [current, setCurrent] = useState<ActionDef | null>(null);
  const a = assignment;
  const available = ACTIONS.filter((d) => d.from.includes(a.status) && canCarPlan(me, d.perm)
    && (d.action !== "release" || !!a.vehicle));
  const canManage = canCarPlan(me, "manage_carplan_assignments");
  const inspectionKind: "handover" | "return" | null = a.status === "allocated" ? "handover"
    : ["active", "suspended", "returning"].includes(a.status) ? "return" : null;

  if (available.length === 0 && !(canManage && inspectionKind)) {
    return <p className="text-xs text-muted">Aucun geste disponible pour votre profil à ce statut.</p>;
  }

  return (
    <div className="space-y-2">
      {a.status === "allocated" && (
        <Notice tone="info">Remise à faire : réalisez l&apos;état des lieux de remise ; le bénéficiaire le validera depuis son espace.</Notice>
      )}
      {a.status === "returning" && (
        <Notice tone="info">Restitution demandée : réalisez l&apos;état des lieux de restitution.</Notice>
      )}
      <div className="flex flex-wrap gap-2">
        {canManage && inspectionKind && (
          <Button size="sm" variant={a.status === "allocated" || a.status === "returning" ? "primary" : "secondary"}
                  onClick={() => onInspection(inspectionKind)}>
            <ClipboardCheck className="h-4 w-4" />
            {inspectionKind === "handover" ? "État des lieux de remise" : "État des lieux de restitution"}
          </Button>
        )}
        {available.map((d) => (
          <Button key={d.action} size="sm" variant={d.variant ?? "secondary"} onClick={() => setCurrent(d)}
                  disabled={d.action === "renew" && !a.planned_end_date}
                  title={d.action === "renew" && !a.planned_end_date ? "Attribution sans fin prévue : prolongez-la ou restituez-la." : undefined}>
            <d.icon className="h-4 w-4" /> {d.label}
          </Button>
        ))}
      </div>
      {current && (
        <ActionDialog key={current.action} def={current} assignment={a} onClose={() => setCurrent(null)}
                      onDone={(msg, created) => { setCurrent(null); onDone(msg, created); }} />
      )}
    </div>
  );
}

function ActionDialog({ def, assignment, onClose, onDone }: {
  def: ActionDef;
  assignment: Assignment;
  onClose: () => void;
  onDone: (message: string, created?: { id: string; reference?: string }) => void;
}) {
  const action = useAssignmentAction();
  const needsVehicle = def.fields.includes("vehicle");
  const expectedMode = MODE_FOR_TYPE[assignment.assignment_type];
  const vehicles = useCarPlanVehicles(expectedMode, needsVehicle);
  const free = (vehicles.data ?? []).filter((v) => !v.holder && v.id !== assignment.vehicle);
  const [values, setValues] = useState<Record<FieldName, string>>({
    note: "", reason: "", vehicle: "", new_end: "", new_end_optional: "", on_date: todayISO(),
    starts_at: nowLocalInput(), ends_at: nowLocalInput(24 * 60),
  });
  const [error, setError] = useState("");
  const set = (k: FieldName, v: string) => setValues((s) => ({ ...s, [k]: v }));

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    const body: Record<string, unknown> = {};
    for (const f of def.fields) {
      const v = values[f].trim();
      if (f === "reason" && !v) { setError("Motif obligatoire."); return; }
      if ((f === "vehicle" || f === "new_end" || f === "on_date" || f === "starts_at" || f === "ends_at") && !v) {
        setError(`${FIELD_LABEL[f]} : champ obligatoire.`);
        return;
      }
      if (f === "note" && v) body.note = v;
      if (f === "reason") body.reason = v;
      if (f === "vehicle") body.vehicle = v;
      if (f === "new_end") body.new_end = v;
      if (f === "new_end_optional" && v) body.new_end = v;
      if (f === "on_date") body.on_date = v;
      if (f === "starts_at") body.starts_at = localInputToISO(v);
      if (f === "ends_at") body.ends_at = localInputToISO(v);
    }
    if (def.action === "release" && new Date(values.ends_at) <= new Date(values.starts_at)) {
      setError("La fin de la mise à disposition doit suivre son début.");
      return;
    }
    action.mutate({ id: assignment.id, action: def.action, body }, {
      onSuccess: (res) => {
        if (def.action === "renew") onDone(`Renouvellement ${res.reference ?? ""} créé : il doit être validé.`, res);
        else if (def.action === "release") onDone("Mise à disposition enregistrée (onglet « Mises à disposition »).");
        else onDone(`${def.title} : c'est fait.`);
      },
      onError: (err) => setError(carPlanError(err)),
    });
  }

  return (
    <Modal open title={`${def.title} — ${assignment.reference}`} onClose={onClose} className="max-w-lg">
      <form onSubmit={submit} className="space-y-3">
        {def.hint && <Notice tone="info">{def.hint}</Notice>}
        {def.fields.map((f) => (
          <div key={f}>
            <Label htmlFor={`act-${f}`}>{FIELD_LABEL[f]}</Label>
            {f === "note" || f === "reason" ? (
              <Textarea id={`act-${f}`} value={values[f]} required={f === "reason"} onChange={(e) => set(f, e.target.value)} />
            ) : f === "vehicle" ? (
              <>
                <Select id="act-vehicle" value={values.vehicle} onChange={(e) => set("vehicle", e.target.value)} required>
                  <option value="">{vehicles.isLoading ? "Chargement…" : "Choisir un véhicule"}</option>
                  {free.map((v) => (
                    <option key={v.id} value={v.id}>{v.registration} — {v.label}{v.subsidiary_name ? ` (${v.subsidiary_name})` : ""}</option>
                  ))}
                </Select>
                {!vehicles.isLoading && free.length === 0 && (
                  <p className="mt-1 text-[11px] text-amber-700 dark:text-amber-300">
                    Aucun véhicule libre en mode « {MODE_LABEL[expectedMode]} » : changez d&apos;abord le mode
                    d&apos;exploitation d&apos;un véhicule (onglet « Véhicules »).
                  </p>
                )}
              </>
            ) : f === "starts_at" || f === "ends_at" ? (
              <Input id={`act-${f}`} type="datetime-local" value={values[f]} required onChange={(e) => set(f, e.target.value)} />
            ) : (
              <Input id={`act-${f}`} type="date" value={values[f]} required={f !== "new_end_optional"}
                     min={f === "new_end" && assignment.planned_end_date ? assignment.planned_end_date : undefined}
                     onChange={(e) => set(f, e.target.value)} />
            )}
          </div>
        ))}
        <FormError message={error} />
        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" variant="secondary" onClick={onClose}>Fermer</Button>
          <Button type="submit" variant={def.variant === "danger" ? "danger" : def.variant === "success" ? "success" : "primary"}
                  disabled={action.isPending}>
            {action.isPending && <Spinner className="h-4 w-4" />} {def.submit ?? def.label}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
