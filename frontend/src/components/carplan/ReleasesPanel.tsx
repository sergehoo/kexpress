"use client";

import { useState } from "react";
import { Ban } from "lucide-react";

import { Modal } from "@/components/Modal";
import { Button, Card, CardBody, EmptyState, Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth";
import { canCarPlan, carPlanError, useReleases, useRevokeRelease, type PoolRelease } from "@/lib/carplan";

import { type Flash, FormError, formatDateTime, Notice, ToneBadge } from "./shared";

function state(r: PoolRelease, now: number): { tone: "slate" | "green" | "violet" | "red"; label: string } {
  if (r.revoked_at) return { tone: "red", label: "Révoquée" };
  if (new Date(r.ends_at).getTime() <= now) return { tone: "slate", label: "Terminée" };
  if (new Date(r.starts_at).getTime() > now) return { tone: "violet", label: "À venir" };
  return { tone: "green", label: "En cours" };
}

/** Mises à disposition TEMPORAIRES de véhicules Car Plan au dispatching (créées depuis le
 *  détail d'une attribution), et leur révocation. */
export function ReleasesPanel({ onOpen }: { onOpen: (assignmentId: string) => void }) {
  const { me } = useAuth();
  const canRevoke = canCarPlan(me, "approve_carplan_assignments");
  const { data, isLoading, error } = useReleases();
  const [revoking, setRevoking] = useState<PoolRelease | null>(null);
  const [flash, setFlash] = useState<Flash>(null);
  const now = Date.now();

  return (
    <div className="space-y-4">
      <Notice tone="info">
        Un véhicule de fonction ou de service n&apos;est jamais proposé au dispatching, sauf mise à disposition autorisée couvrant
        tout le créneau demandé. Pour en créer une, ouvrez l&apos;attribution et choisissez « Mettre à disposition ».
      </Notice>
      {flash && <Notice tone={flash.tone}>{flash.text}</Notice>}
      <Card>
        <CardBody className="p-0">
          {isLoading ? <div className="flex justify-center py-16"><Spinner className="h-7 w-7" /></div>
            : error ? <div className="p-4"><Notice tone="danger">{carPlanError(error)}</Notice></div>
            : !(data ?? []).length ? <EmptyState title="Aucune mise à disposition" />
            : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead><tr className="border-b border-line text-left text-xs uppercase tracking-wide text-faint">
                    <th className="px-4 py-3 font-medium">Véhicule</th><th className="px-4 py-3 font-medium">Du</th>
                    <th className="px-4 py-3 font-medium">Au</th><th className="px-4 py-3 font-medium">Motif</th>
                    <th className="px-4 py-3 font-medium">État</th><th className="px-4 py-3 text-right font-medium">Actions</th>
                  </tr></thead>
                  <tbody className="divide-y divide-line">
                    {(data ?? []).map((r) => {
                      const s = state(r, now);
                      const active = !r.revoked_at && new Date(r.ends_at).getTime() > now;
                      return (
                        <tr key={r.id}>
                          <td className="px-4 py-3 font-medium text-ink">{r.vehicle_registration}
                            {r.assignment && (
                              <button className="block text-[11px] font-normal text-brand-600 hover:underline" onClick={() => onOpen(r.assignment!)}>
                                Voir l&apos;attribution
                              </button>
                            )}
                          </td>
                          <td className="whitespace-nowrap px-4 py-3 text-muted">{formatDateTime(r.starts_at)}</td>
                          <td className="whitespace-nowrap px-4 py-3 text-muted">{formatDateTime(r.ends_at)}</td>
                          <td className="px-4 py-3 text-xs text-muted">{r.reason}</td>
                          <td className="px-4 py-3"><ToneBadge tone={s.tone} label={s.label} />
                            {r.revoked_at && <span className="block text-[10px] text-faint">{formatDateTime(r.revoked_at)}</span>}</td>
                          <td className="px-4 py-3 text-right">
                            {canRevoke && active && (
                              <Button size="sm" variant="secondary" onClick={() => setRevoking(r)}><Ban className="h-3.5 w-3.5" /> Révoquer</Button>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
        </CardBody>
      </Card>
      {revoking && (
        <RevokeDialog release={revoking} onClose={() => setRevoking(null)}
                      onDone={() => { setRevoking(null); setFlash({ tone: "success", text: "Mise à disposition révoquée : le véhicule n'est plus proposé au dispatching." }); }} />
      )}
    </div>
  );
}

function RevokeDialog({ release, onClose, onDone }: { release: PoolRelease; onClose: () => void; onDone: () => void }) {
  const revoke = useRevokeRelease();
  const [error, setError] = useState("");
  return (
    <Modal open title="Révoquer la mise à disposition" onClose={onClose}>
      <p className="text-sm text-muted">
        {release.vehicle_registration} ne sera plus proposé au dispatching du {formatDateTime(release.starts_at)} au {formatDateTime(release.ends_at)}.
        Les courses déjà affectées ne sont pas modifiées.
      </p>
      <div className="pt-3"><FormError message={error} /></div>
      <div className="flex justify-end gap-2 pt-3">
        <Button variant="secondary" onClick={onClose}>Annuler</Button>
        <Button variant="danger" disabled={revoke.isPending} onClick={() => revoke.mutate(release.id, {
          onSuccess: onDone, onError: (err) => setError(carPlanError(err)),
        })}>{revoke.isPending && <Spinner className="h-4 w-4" />} Révoquer</Button>
      </div>
    </Modal>
  );
}
