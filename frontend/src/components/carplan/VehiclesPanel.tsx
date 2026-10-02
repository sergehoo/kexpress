"use client";

import { useState } from "react";
import { ArrowRightLeft, Check, Search, X } from "lucide-react";

import { Modal } from "@/components/Modal";
import { Button, Card, CardBody, CardHeader, CardTitle, EmptyState, Input, Label, Select, Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth";
import {
  canCarPlan, carPlanError, FUEL_LABEL, MODE_CHANGE_STATUS_LABEL, MODE_CHANGE_STATUS_TONE, MODE_LABEL, MODE_TONE,
  useCarPlanVehicles, useDecideModeChange, useModeChanges, useRequestModeChange, VEHICLE_TYPES,
  type ModeChange, type VehicleMode, type VehicleModeRow,
} from "@/lib/carplan";

import { type Flash, FormError, formatDateTime, Notice, Textarea, ToneBadge } from "./shared";

const MODES = Object.entries(MODE_LABEL) as [VehicleMode, string][];
const typeLabel = (v: string) => VEHICLE_TYPES.find((t) => t.value === v)?.label ?? v;

/** Modes d'exploitation des véhicules du périmètre et changements (demande → décision par
 *  une autre personne). */
export function VehiclesPanel() {
  const { me } = useAuth();
  const canModes = canCarPlan(me, "manage_carplan_vehicle_modes");
  const [mode, setMode] = useState<VehicleMode | "">("");
  const [search, setSearch] = useState("");
  const [requesting, setRequesting] = useState<VehicleModeRow | null>(null);
  const [deciding, setDeciding] = useState<{ change: ModeChange; approve: boolean } | null>(null);
  const [flash, setFlash] = useState<Flash>(null);
  const vehicles = useCarPlanVehicles(mode);
  const changes = useModeChanges();

  const q = search.trim().toLowerCase();
  const list = (vehicles.data ?? []).filter((v) => !q || v.registration.toLowerCase().includes(q)
    || v.label.toLowerCase().includes(q) || (v.holder ?? "").toLowerCase().includes(q));
  const pending = (changes.data ?? []).filter((c) => c.status === "requested");
  const decided = (changes.data ?? []).filter((c) => c.status !== "requested").slice(0, 30);

  return (
    <div className="space-y-5">
      {flash && <Notice tone={flash.tone}>{flash.text}</Notice>}

      {pending.length > 0 && (
        <Card>
          <CardHeader><CardTitle>Changements de mode en attente ({pending.length})</CardTitle></CardHeader>
          <CardBody className="p-0">
            <ul className="divide-y divide-line">
              {pending.map((c) => (
                <li key={c.id} className="flex flex-wrap items-center gap-3 px-5 py-3">
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium text-ink">
                      {c.vehicle_registration} : {MODE_LABEL[c.from_mode]} → {MODE_LABEL[c.to_mode]}
                    </p>
                    <p className="text-xs text-muted">{c.reason}</p>
                    <p className="text-[11px] text-faint">Demandé par {c.requested_by_name} · {formatDateTime(c.created_at)}</p>
                  </div>
                  {canModes && (
                    <div className="flex gap-2">
                      <Button size="sm" variant="success" onClick={() => setDeciding({ change: c, approve: true })}>
                        <Check className="h-3.5 w-3.5" /> Appliquer
                      </Button>
                      <Button size="sm" variant="danger" onClick={() => setDeciding({ change: c, approve: false })}>
                        <X className="h-3.5 w-3.5" /> Refuser
                      </Button>
                    </div>
                  )}
                </li>
              ))}
            </ul>
            <p className="px-5 pb-3 text-[11px] text-faint">La décision revient à une autre personne que le demandeur.</p>
          </CardBody>
        </Card>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <div className="relative w-full sm:w-64">
          <Search className="pointer-events-none absolute left-3 top-3 h-4 w-4 text-faint" />
          <Input value={search} onChange={(e) => setSearch(e.target.value)} className="pl-9"
                 placeholder="Immatriculation, modèle, détenteur…" aria-label="Rechercher un véhicule" />
        </div>
        <Select value={mode} onChange={(e) => setMode(e.target.value as VehicleMode | "")} className="sm:w-60" aria-label="Mode">
          <option value="">Tous les modes</option>
          {MODES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </Select>
      </div>

      <Card>
        <CardBody className="p-0">
          {vehicles.isLoading ? <div className="flex justify-center py-16"><Spinner className="h-7 w-7" /></div>
            : vehicles.error ? <div className="p-4"><Notice tone="danger">{carPlanError(vehicles.error)}</Notice></div>
            : list.length === 0 ? <EmptyState title="Aucun véhicule" />
            : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead><tr className="border-b border-line text-left text-xs uppercase tracking-wide text-faint">
                    <th className="px-4 py-3 font-medium">Véhicule</th><th className="px-4 py-3 font-medium">Type</th>
                    <th className="px-4 py-3 font-medium">Filiale</th><th className="px-4 py-3 font-medium">Mode</th>
                    <th className="px-4 py-3 font-medium">Détenteur</th><th className="px-4 py-3 text-right font-medium">Actions</th>
                  </tr></thead>
                  <tbody className="divide-y divide-line">
                    {list.map((v) => (
                      <tr key={v.id}>
                        <td className="px-4 py-3"><span className="font-medium text-ink">{v.registration}</span>
                          <span className="block text-[11px] text-muted">{v.label}</span></td>
                        <td className="px-4 py-3 text-xs text-muted">{typeLabel(v.vehicle_type)} · {FUEL_LABEL[v.fuel_type] ?? v.fuel_type}</td>
                        <td className="px-4 py-3 text-muted">{v.subsidiary_name ?? "—"}</td>
                        <td className="px-4 py-3">
                          <ToneBadge tone={MODE_TONE[v.mode] ?? "slate"} label={v.mode_display} />
                          {v.pending_change && (
                            <span className="ml-1 text-[11px] text-amber-700 dark:text-amber-300">→ {MODE_LABEL[v.pending_change.to_mode]} (en attente)</span>
                          )}
                        </td>
                        <td className="px-4 py-3 text-muted">{v.holder ?? "—"}</td>
                        <td className="px-4 py-3 text-right">
                          {canModes && !v.pending_change && (
                            <Button size="sm" variant="secondary" onClick={() => { setFlash(null); setRequesting(v); }}>
                              <ArrowRightLeft className="h-3.5 w-3.5" /> Changer de mode
                            </Button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
        </CardBody>
      </Card>

      {decided.length > 0 && (
        <Card>
          <CardHeader><CardTitle>Historique des changements de mode</CardTitle></CardHeader>
          <CardBody className="p-0">
            <ul className="divide-y divide-line">
              {decided.map((c) => (
                <li key={c.id} className="flex flex-wrap items-center gap-2 px-5 py-2.5 text-sm">
                  <span className="font-medium text-ink">{c.vehicle_registration}</span>
                  <span className="text-muted">{MODE_LABEL[c.from_mode]} → {MODE_LABEL[c.to_mode]}</span>
                  <ToneBadge tone={MODE_CHANGE_STATUS_TONE[c.status] ?? "slate"} label={MODE_CHANGE_STATUS_LABEL[c.status] ?? c.status} />
                  <span className="ml-auto text-[11px] text-faint">
                    {c.decided_by_name ?? "—"} · {formatDateTime(c.decided_at)}{c.decision_note ? ` · ${c.decision_note}` : ""}
                  </span>
                </li>
              ))}
            </ul>
          </CardBody>
        </Card>
      )}

      {requesting && (
        <RequestModeDialog vehicle={requesting} onClose={() => setRequesting(null)}
                           onDone={() => { setRequesting(null); setFlash({ tone: "success", text: "Changement de mode demandé : il sera appliqué après décision d'une autre personne." }); }} />
      )}
      {deciding && (
        <DecideDialog change={deciding.change} approve={deciding.approve} onClose={() => setDeciding(null)}
                      onDone={(applied) => { setDeciding(null); setFlash({ tone: "success", text: applied ? "Changement de mode appliqué." : "Changement de mode refusé." }); }} />
      )}
    </div>
  );
}

function RequestModeDialog({ vehicle, onClose, onDone }: { vehicle: VehicleModeRow; onClose: () => void; onDone: () => void }) {
  const request = useRequestModeChange();
  const [toMode, setToMode] = useState<VehicleMode | "">("");
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  return (
    <Modal open title={`Changer le mode — ${vehicle.registration}`} onClose={onClose}>
      <form className="space-y-3" onSubmit={(e) => {
        e.preventDefault();
        setError("");
        if (!toMode || !reason.trim()) { setError("Nouveau mode et motif obligatoires."); return; }
        request.mutate({ vehicleId: vehicle.id, to_mode: toMode, reason: reason.trim() }, {
          onSuccess: onDone, onError: (err) => setError(carPlanError(err)),
        });
      }}>
        <p className="text-xs text-muted">Mode actuel : <span className="font-medium text-ink">{vehicle.mode_display}</span>.
          Un véhicule de fonction ou de service n&apos;est plus proposé au dispatching (sauf mise à disposition autorisée).</p>
        <div><Label htmlFor="rm-mode">Nouveau mode</Label>
          <Select id="rm-mode" value={toMode} onChange={(e) => setToMode(e.target.value as VehicleMode)} required>
            <option value="">Choisir</option>
            {MODES.filter(([v]) => v !== vehicle.mode).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </Select></div>
        <div><Label htmlFor="rm-reason">Motif</Label><Textarea id="rm-reason" value={reason} required onChange={(e) => setReason(e.target.value)} /></div>
        <FormError message={error} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose}>Annuler</Button>
          <Button type="submit" disabled={request.isPending}>{request.isPending && <Spinner className="h-4 w-4" />} Demander</Button>
        </div>
      </form>
    </Modal>
  );
}

function DecideDialog({ change, approve, onClose, onDone }: {
  change: ModeChange; approve: boolean; onClose: () => void; onDone: (applied: boolean) => void;
}) {
  const decide = useDecideModeChange();
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  return (
    <Modal open title={approve ? "Appliquer le changement de mode" : "Refuser le changement de mode"} onClose={onClose}>
      <div className="space-y-3">
        <p className="text-sm text-ink">{change.vehicle_registration} : {MODE_LABEL[change.from_mode]} → {MODE_LABEL[change.to_mode]}</p>
        <p className="text-xs text-muted">{change.reason}</p>
        <div><Label htmlFor="dm-note">Observation (facultative)</Label><Textarea id="dm-note" value={note} onChange={(e) => setNote(e.target.value)} /></div>
        <FormError message={error} />
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>Annuler</Button>
          <Button variant={approve ? "success" : "danger"} disabled={decide.isPending}
                  onClick={() => decide.mutate({ id: change.id, approve, note: note.trim() }, {
                    onSuccess: (res) => onDone(res.status === "applied"), onError: (err) => setError(carPlanError(err)),
                  })}>
            {decide.isPending && <Spinner className="h-4 w-4" />} {approve ? "Appliquer" : "Refuser"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
