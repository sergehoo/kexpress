"use client";

import { useState } from "react";
import Link from "next/link";
import { CheckCircle2, ChevronRight, Clock, Fuel, Gauge, Route } from "lucide-react";

import { Button, Card, CardBody, EmptyState, Select, Spinner } from "@/components/ui";
import { StatusBadge } from "@/components/StatusBadge";
import { StatChips } from "@/components/StatChips";
import { EndTripModal, StartTripModal } from "@/components/trip-modals";
import { useTripAction, useTrips } from "@/lib/queries";
import { apiError } from "@/lib/api";
import type { Trip } from "@/lib/types";
import { formatDate, formatNumber } from "@/lib/utils";

type ModalState = { type: "start" | "end"; trip: Trip } | null;

export default function TripsPage() {
  const [status, setStatus] = useState("");
  const [modal, setModal] = useState<ModalState>(null);
  const [toast, setToast] = useState("");

  const params: Record<string, string> = {};
  if (status) params.status = status;
  const { data, isLoading } = useTrips(params);
  const list = data?.results ?? [];

  const close = useTripAction("close");

  // Statistiques de la liste affichée
  const inProgress = list.filter((t) => t.status === "in_progress").length;
  const done = list.filter((t) => ["returned", "closed"].includes(t.status)).length;
  const totalKm = list.reduce((s, t) => s + Number(t.distance_km ?? 0), 0);
  const totalFuel = list.reduce((s, t) => s + Number(t.fuel_consumed ?? 0), 0);

  return (
    <div className="space-y-5">
      <StatChips
        stats={[
          { label: "Courses affichées", value: list.length, icon: Route, tone: "bg-brand-500/10 text-brand-600" },
          { label: "En cours", value: inProgress, icon: Clock, tone: "bg-sky-500/10 text-sky-600" },
          { label: "Terminées", value: done, icon: CheckCircle2, tone: "bg-emerald-500/10 text-emerald-600" },
          { label: "Km cumulés", value: formatNumber(totalKm, "km"), icon: Gauge, tone: "bg-cyan-500/10 text-cyan-600" },
          { label: "Carburant réel", value: `${formatNumber(totalFuel)} L`, icon: Fuel, tone: "bg-orange-500/10 text-orange-600" },
        ]}
      />

      <Select value={status} onChange={(e) => setStatus(e.target.value)} className="sm:w-64">
        <option value="">Toutes les courses</option>
        <option value="scheduled">Planifiée</option>
        <option value="in_progress">En cours</option>
        <option value="returned">Retour effectué</option>
        <option value="closed">Clôturée</option>
      </Select>

      {toast && (
        <div className="flex items-center justify-between rounded-lg bg-rose-50 px-4 py-2 text-sm text-rose-700">
          {toast}
          <button onClick={() => setToast("")} className="text-rose-400">✕</button>
        </div>
      )}

      {isLoading ? (
        <div className="flex justify-center py-16">
          <Spinner className="h-7 w-7" />
        </div>
      ) : list.length === 0 ? (
        <Card>
          <CardBody>
            <EmptyState title="Aucune course" hint="Les courses sont créées à l'affectation d'un véhicule." />
          </CardBody>
        </Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          {list.map((t) => (
            <Card key={t.id}>
              <CardBody className="space-y-3">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <Link href={`/trips/${t.id}`} className="block truncate font-medium text-ink hover:text-brand-600 hover:underline">
                      {t.destination}
                    </Link>
                    <p className="text-xs text-muted">
                      {t.vehicle_registration}
                      {t.driver_name ? ` · ${t.driver_name}` : ""}
                    </p>
                  </div>
                  <StatusBadge code={t.status} label={t.status_display} />
                </div>

                <div className="grid grid-cols-2 gap-2 text-xs text-muted">
                  <span>Départ : {formatDate(t.actual_departure, true)}</span>
                  <span>Retour : {formatDate(t.actual_return, true)}</span>
                  <span className="flex items-center gap-1">
                    <Gauge className="h-3.5 w-3.5" />
                    {t.distance_km ? formatNumber(t.distance_km, "km") : "—"}
                  </span>
                  <span>
                    ⛽ {t.estimated_fuel_l != null ? `est. ${t.estimated_fuel_l} L` : "—"}
                    {t.fuel_consumed ? ` · réel ${t.fuel_consumed} L` : ""}
                  </span>
                </div>

                {/* Fuel Intelligence — gestionnaires uniquement (null pour l'employé) */}
                {t.fuel_intel && (t.fuel_intel.real_l != null || t.fuel_intel.fuel_cost != null) && (
                  <div className="rounded-lg bg-surface2 px-3 py-2 text-[11px] text-muted">
                    <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                      {t.fuel_intel.gap_pct != null && (
                        <span className={t.fuel_intel.gap_pct > 15 ? "font-medium text-rose-600" : t.fuel_intel.gap_pct < -5 ? "font-medium text-emerald-600" : ""}>
                          Écart : {t.fuel_intel.gap_pct > 0 ? "+" : ""}{t.fuel_intel.gap_pct}%
                        </span>
                      )}
                      {t.fuel_intel.efficiency_score != null && (
                        <span>Score énergie : <span className="font-semibold text-ink">{t.fuel_intel.efficiency_score}/100</span></span>
                      )}
                      {t.fuel_intel.fuel_cost != null && (
                        <span>Coût : <span className="font-semibold text-ink">{formatNumber(t.fuel_intel.fuel_cost)} XOF</span></span>
                      )}
                      {t.fuel_intel.fuel_price != null && (
                        <span className="text-faint">(prix {formatNumber(t.fuel_intel.fuel_price)}/L au {formatDate(t.fuel_intel.fuel_price_date)})</span>
                      )}
                    </div>
                  </div>
                )}

                <div className="flex flex-wrap gap-2 border-t border-line pt-3">
                  {t.status === "scheduled" && (
                    <Button size="sm" onClick={() => setModal({ type: "start", trip: t })}>
                      Démarrer
                    </Button>
                  )}
                  {t.status === "in_progress" && (
                    <Button size="sm" variant="success" onClick={() => setModal({ type: "end", trip: t })}>
                      Terminer
                    </Button>
                  )}
                  {t.status === "returned" && (
                    <Button
                      size="sm"
                      variant="secondary"
                      disabled={close.isPending}
                      onClick={() =>
                        close.mutate({ id: t.id }, { onError: (e) => setToast(apiError(e)) })
                      }
                    >
                      Clôturer
                    </Button>
                  )}
                  <Link
                    href={`/trips/${t.id}`}
                    className="ml-auto inline-flex items-center gap-0.5 rounded-md px-2 py-1.5 text-xs font-medium text-muted hover:bg-surface2 hover:text-ink"
                  >
                    Détails <ChevronRight className="h-3.5 w-3.5" />
                  </Link>
                </div>
              </CardBody>
            </Card>
          ))}
        </div>
      )}

      {modal?.type === "start" && (
        <StartTripModal trip={modal.trip} onClose={() => setModal(null)} onError={setToast} />
      )}
      {modal?.type === "end" && (
        <EndTripModal trip={modal.trip} onClose={() => setModal(null)} onError={setToast} />
      )}
    </div>
  );
}
