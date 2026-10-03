"use client";

import { useState } from "react";
import { Pencil, RefreshCcw } from "lucide-react";

import { Modal } from "@/components/Modal";
import { Button, EmptyState, Input, Label, Select, Spinner } from "@/components/ui";
import {
  carPlanError, READING_FREQUENCIES, useAssignmentMileage, useAssignmentTracking, useCorrectMileage,
  useMeterReplacement, useReadingFrequency, type Assignment, type MileageReading,
} from "@/lib/carplan";
import { cn, formatNumber } from "@/lib/utils";

import { MaintenanceCalendar, TrackingOverview } from "./MileageTracking";
import { FormError, formatDateTime, km, localInputToISO, nowLocalInput, SectionTitle } from "./shared";

type FlashFn = (text: string, tone?: "success" | "danger") => void;
const IN_USE = ["active", "suspended", "returning"];

/** Gestion : suivi kilométrique d'une attribution (fréquence, retard, rythme, fiabilité),
 *  remplacement de compteur et calendrier des entretiens du véhicule. Aucun montant. */
export function AssignmentTrackingPanel({ a, canManage, onFlash }: { a: Assignment; canManage: boolean; onFlash: FlashFn }) {
  const tracking = useAssignmentTracking(a.id, !!a.vehicle);
  const frequency = useReadingFrequency();
  const [meterOpen, setMeterOpen] = useState(false);
  const t = tracking.data;
  const running = IN_USE.includes(a.status) && !!a.vehicle;

  if (!a.vehicle) return null;
  return (
    <section className="space-y-3 rounded-xl border border-line bg-surface p-4">
      <SectionTitle action={canManage && running ? (
        <Button size="sm" variant="secondary" onClick={() => setMeterOpen(true)}>
          <RefreshCcw className="h-3.5 w-3.5" /> Remplacement de compteur
        </Button>
      ) : undefined}>Suivi kilométrique</SectionTitle>
      {tracking.isLoading ? <Spinner /> : !t ? <p className="text-xs text-muted">Suivi indisponible.</p> : (
        <>
          <TrackingOverview tracking={t} />
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
            <span>Fréquence des relevés :</span>
            {canManage && running ? (
              <Select aria-label="Fréquence des relevés" className="h-8 w-auto text-xs"
                      value={a.reading_frequency_days ?? ""} disabled={frequency.isPending}
                      onChange={(e) => frequency.mutate(
                        { assignmentId: a.id, days: e.target.value ? Number(e.target.value) : null },
                        { onSuccess: () => onFlash("Fréquence des relevés mise à jour."), onError: (err) => onFlash(carPlanError(err), "danger") })}>
                <option value="">Celle de la politique ({t.reading.frequency_source === "politique" ? t.reading.frequency_days : "7"} j)</option>
                {READING_FREQUENCIES.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
              </Select>
            ) : <span className="font-medium text-ink">tous les {t.reading.frequency_days} jours ({t.reading.frequency_source})</span>}
            {t.last_reminder && <span>· {t.last_reminder.label} le {formatDateTime(t.last_reminder.at)}</span>}
          </div>
          <div>
            <p className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-faint">Calendrier des entretiens</p>
            <MaintenanceCalendar rows={t.maintenance} />
          </div>
        </>
      )}
      {meterOpen && <MeterReplacementDialog a={a} lastOdometer={t?.reading.last_reading?.odometer ?? null}
                                            onClose={() => setMeterOpen(false)}
                                            onDone={() => { setMeterOpen(false); onFlash("Remplacement de compteur enregistré : seuils d'entretien reportés sur le nouveau compteur."); }} />}
    </section>
  );
}

/** Historique horodaté des relevés, corrections comprises ; correction tracée par le gestionnaire. */
export function ManagerReadingsTable({ a, canManage, onFlash }: { a: Assignment; canManage: boolean; onFlash: FlashFn }) {
  const readings = useAssignmentMileage(a.id);
  const [fixing, setFixing] = useState<MileageReading | null>(null);
  const rows = [...(readings.data ?? [])].reverse();

  return (
    <section className="rounded-xl border border-line bg-surface">
      <div className="px-4 pt-4"><SectionTitle>Relevés kilométriques</SectionTitle></div>
      {readings.isLoading ? <div className="flex justify-center py-6"><Spinner /></div>
        : !rows.length ? <EmptyState title="Aucun relevé" />
        : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead><tr className="border-b border-line text-left text-[11px] uppercase tracking-wide text-faint">
                <th className="px-4 py-2 font-medium">Relevé le</th><th className="px-4 py-2 font-medium">Véhicule</th>
                <th className="px-4 py-2 text-right font-medium">Compteur</th><th className="px-4 py-2 text-right font-medium">Pro</th>
                <th className="px-4 py-2 text-right font-medium">Privé</th><th className="px-4 py-2 font-medium">Origine</th>
                {canManage && <th className="px-4 py-2" />}
              </tr></thead>
              <tbody className="divide-y divide-line">
                {rows.map((r) => (
                  <tr key={r.id} className={cn(r.superseded && "opacity-60")}>
                    <td className="whitespace-nowrap px-4 py-2 text-muted">{formatDateTime(r.recorded_at)}</td>
                    <td className="px-4 py-2 text-muted">{r.vehicle_registration}</td>
                    <td className={cn("whitespace-nowrap px-4 py-2 text-right font-medium text-ink", r.superseded && "line-through")}>{km(r.odometer)}</td>
                    <td className="px-4 py-2 text-right text-muted">{r.professional_km != null ? formatNumber(r.professional_km) : "—"}</td>
                    <td className="px-4 py-2 text-right text-muted">{r.private_km != null ? formatNumber(r.private_km) : "—"}</td>
                    <td className="px-4 py-2 text-xs text-muted">
                      {r.source_display}
                      {r.previous_odometer != null && <span className="block text-[11px] text-faint">ancien compteur {km(r.previous_odometer)}</span>}
                      {r.superseded && <span className="block text-[11px] text-amber-700 dark:text-amber-300">corrigé</span>}
                      {r.corrects && <span className="block text-[11px] text-sky-700 dark:text-sky-300">correction{r.reason ? ` : ${r.reason}` : ""}</span>}
                      {r.source === "meter_replacement" && r.reason && <span className="block text-[11px] text-faint">{r.reason}</span>}
                      {r.anomaly && !r.superseded && <span className="block text-[11px] text-amber-700 dark:text-amber-300">{r.anomaly}</span>}
                    </td>
                    {canManage && (
                      <td className="px-4 py-2 text-right">
                        {!r.superseded && (r.source === "declaration" || r.source === "manager") && (
                          <button type="button" className="inline-flex items-center gap-1 text-xs text-brand-600 hover:underline"
                                  onClick={() => setFixing(r)}><Pencil className="h-3 w-3" /> Corriger</button>
                        )}
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      {fixing && <CorrectionDialog a={a} reading={fixing} onClose={() => setFixing(null)}
                                   onDone={() => { setFixing(null); onFlash("Relevé corrigé ; le bénéficiaire en est informé."); }} />}
    </section>
  );
}

function CorrectionDialog({ a, reading, onClose, onDone }: {
  a: Assignment; reading: MileageReading; onClose: () => void; onDone: () => void;
}) {
  const correct = useCorrectMileage();
  const [odometer, setOdometer] = useState(String(reading.odometer));
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    const n = Number(odometer);
    if (!Number.isInteger(n) || n < 0) { setError("Compteur en km (nombre entier)."); return; }
    if (!reason.trim()) { setError("Motif de correction obligatoire."); return; }
    correct.mutate({ assignmentId: a.id, readingId: reading.id, odometer: n, reason: reason.trim() },
      { onSuccess: onDone, onError: (err) => setError(carPlanError(err)) });
  }
  return (
    <Modal open title={`Corriger le relevé du ${formatDateTime(reading.recorded_at)}`} onClose={onClose}>
      <form onSubmit={submit} className="space-y-3">
        <p className="text-xs text-muted">Valeur saisie : {km(reading.odometer)}. La correction crée un nouveau relevé ; l&apos;original reste
          dans l&apos;historique. La valeur doit rester comprise entre les relevés voisins.</p>
        <div><Label htmlFor="fix-mgr-odo">Compteur corrigé (km)</Label>
          <Input id="fix-mgr-odo" type="number" min={0} inputMode="numeric" value={odometer} onChange={(e) => setOdometer(e.target.value)} /></div>
        <div><Label htmlFor="fix-mgr-reason">Motif</Label>
          <Input id="fix-mgr-reason" value={reason} maxLength={500} required onChange={(e) => setReason(e.target.value)} /></div>
        <FormError message={error} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose}>Annuler</Button>
          <Button type="submit" disabled={correct.isPending}>{correct.isPending && <Spinner className="h-4 w-4" />} Corriger</Button>
        </div>
      </form>
    </Modal>
  );
}

function MeterReplacementDialog({ a, lastOdometer, onClose, onDone }: {
  a: Assignment; lastOdometer: number | null; onClose: () => void; onDone: () => void;
}) {
  const replace = useMeterReplacement();
  const [oldOdometer, setOldOdometer] = useState(lastOdometer != null ? String(lastOdometer) : "");
  const [newOdometer, setNewOdometer] = useState("0");
  const [reason, setReason] = useState("");
  const [at, setAt] = useState(nowLocalInput());
  const [error, setError] = useState("");
  function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    const o = Number(oldOdometer);
    const n = Number(newOdometer);
    if (!Number.isInteger(o) || !Number.isInteger(n) || o < 0 || n < 0) { setError("Kilométrages entiers et positifs."); return; }
    if (!reason.trim()) { setError("Motif obligatoire."); return; }
    replace.mutate({ assignmentId: a.id, body: { old_odometer: o, new_odometer: n, reason: reason.trim(), recorded_at: localInputToISO(at) } },
      { onSuccess: onDone, onError: (err) => setError(carPlanError(err)) });
  }
  return (
    <Modal open title="Remplacement de compteur" onClose={onClose}>
      <form onSubmit={submit} className="space-y-3">
        <p className="text-xs text-muted">
          Le dernier relevé de l&apos;ancien compteur clôt sa série ; la nouvelle base ouvre la suivante. Les moyennes restent
          calculées sur la distance réelle et les seuils d&apos;entretien sont reportés sur le nouveau compteur.
        </p>
        <div className="grid gap-3 sm:grid-cols-2">
          <div><Label htmlFor="meter-old">Ancien compteur — dernier relevé (km)</Label>
            <Input id="meter-old" type="number" min={lastOdometer ?? 0} inputMode="numeric" value={oldOdometer} onChange={(e) => setOldOdometer(e.target.value)} /></div>
          <div><Label htmlFor="meter-new">Nouveau compteur — base (km)</Label>
            <Input id="meter-new" type="number" min={0} inputMode="numeric" value={newOdometer} onChange={(e) => setNewOdometer(e.target.value)} /></div>
        </div>
        <div><Label htmlFor="meter-at">Date et heure du remplacement</Label>
          <Input id="meter-at" type="datetime-local" value={at} max={nowLocalInput(5)} onChange={(e) => setAt(e.target.value)} /></div>
        <div><Label htmlFor="meter-reason">Motif</Label>
          <Input id="meter-reason" value={reason} maxLength={500} required placeholder="Ex. combiné d'instruments remplacé"
                 onChange={(e) => setReason(e.target.value)} /></div>
        <FormError message={error} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose}>Annuler</Button>
          <Button type="submit" disabled={replace.isPending}>{replace.isPending && <Spinner className="h-4 w-4" />} Enregistrer</Button>
        </div>
      </form>
    </Modal>
  );
}
