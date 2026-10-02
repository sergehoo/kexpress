"use client";

import { useEffect, useState } from "react";
import { AlertTriangle, Car, Coins, Fuel, Lock, Receipt, TrendingDown, Wrench } from "lucide-react";

import { Button, Card, CardBody, CardHeader, CardTitle, EmptyState, Select, Spinner } from "@/components/ui";
import {
  useClosePeriod, useFinancePeriods, useSubsidiaryCosts, useTripCostSheets, useVehicleCosts,
  type Money, type TripCostSheet,
} from "@/lib/queries";
import { apiError } from "@/lib/api";
import { cn, formatDate, formatNumber } from "@/lib/utils";

/** null = inconnu (jamais affiché comme 0). */
export function money(value: Money | undefined, currency = "XOF") {
  return value != null ? `${formatNumber(value)} ${currency}` : "—";
}

function pct(value: Money | undefined) {
  return value != null ? `${Math.round(Number(value) * 100)} %` : "—";
}

const MISSING: Record<string, string> = {
  energy: "énergie", driver: "chauffeur", distance: "distance", insurance: "assurance",
  insurance_period: "période d'assurance", acquisition: "acquisition", monthly_payment: "loyer",
  depreciation_basis: "base d'amortissement", maintenance: "maintenance", tyres: "pneumatiques",
  subscriptions: "abonnements", taxes: "taxes", other_fixed: "autres charges",
};

const STATUS: Record<TripCostSheet["status"], string> = {
  pending: "En calcul", direct_frozen: "Direct figé", complete: "Complet",
};

function Kpi({ icon: Icon, label, value, sub, tone }: {
  icon: React.ElementType; label: string; value: string; sub?: string; tone: string;
}) {
  return (
    <Card>
      <CardBody className="flex items-center gap-3 py-3">
        <span className={cn("flex h-10 w-10 shrink-0 items-center justify-center rounded-xl", tone)}>
          <Icon className="h-5 w-5" />
        </span>
        <div className="min-w-0">
          <p className="truncate text-lg font-semibold leading-tight text-ink">{value}</p>
          <p className="text-[11px] text-muted">{label}</p>
          {sub && <p className="text-[11px] text-faint">{sub}</p>}
        </div>
      </CardBody>
    </Card>
  );
}

/** Coût réel (F1) : ce que la flotte a réellement coûté, distinct du barème kilométrique.
 *  Mois ouvert → chiffres provisoires ; mois clos → figés. */
export function RealCostPanel({ subsidiary, canClose }: { subsidiary: string; canClose: boolean }) {
  const periods = useFinancePeriods();
  const [period, setPeriod] = useState("");
  const [error, setError] = useState("");
  const close = useClosePeriod();

  useEffect(() => {
    // Par défaut : le dernier mois terminé (le mois en cours n'est jamais complet).
    if (!period && periods.data?.length) setPeriod(periods.data[Math.min(1, periods.data.length - 1)].period);
  }, [period, periods.data]);

  const summary = useSubsidiaryCosts(period, subsidiary);
  const vehicles = useVehicleCosts(period, subsidiary);
  const trips = useTripCostSheets(period, subsidiary);
  const current = periods.data?.find((p) => p.period === period);
  const s = summary.data;

  function onClose() {
    if (!current) return;
    if (!window.confirm(`Clôturer ${current.period} ? Les coûts du mois seront figés définitivement.`)) return;
    setError("");
    close.mutate(current.period, { onError: (e) => setError(apiError(e)) });
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        <Select value={period} onChange={(e) => setPeriod(e.target.value)} className="sm:w-48">
          {(periods.data ?? []).map((p) => (
            <option key={p.period} value={p.period}>{p.period}{p.status === "closed" ? " · clos" : ""}</option>
          ))}
        </Select>
        {current?.status === "closed" ? (
          <span className="inline-flex items-center gap-1 rounded-full bg-emerald-500/10 px-2.5 py-1 text-xs font-medium text-emerald-700">
            <Lock className="h-3.5 w-3.5" /> Mois clos le {formatDate(current.closed_at ?? "", true)}{current.closed_by ? ` par ${current.closed_by}` : ""}
          </span>
        ) : (
          <span className="rounded-full bg-amber-500/10 px-2.5 py-1 text-xs font-medium text-amber-700">
            Provisoire : charges indirectes imputées à la clôture du mois
          </span>
        )}
        {canClose && current?.can_close && (
          <Button className="ml-auto" variant="secondary" onClick={onClose} disabled={close.isPending}>
            <Lock className="h-4 w-4" /> Clôturer le mois
          </Button>
        )}
      </div>
      {error && <p className="text-sm text-red-600">{error}</p>}

      {summary.isLoading || !s ? (
        <div className="flex justify-center py-12"><Spinner className="h-7 w-7" /></div>
      ) : (
        <>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Kpi icon={Car} label="Coût des courses" value={money(s.trips_cost, s.currency)}
                 sub={`${s.trips_count} course(s) · ${s.trips_incomplete} incomplète(s)`} tone="bg-brand-500/10 text-brand-600" />
            <Kpi icon={Fuel} label="Énergie" value={money(s.energy, s.currency)} tone="bg-sky-500/10 text-sky-600" />
            <Kpi icon={Wrench} label="Maintenance" value={money(s.maintenance, s.currency)} tone="bg-violet-500/10 text-violet-600" />
            <Kpi icon={Receipt} label="Dépenses directes" value={money(s.expenses, s.currency)} tone="bg-amber-500/10 text-amber-600" />
            <Kpi icon={Coins} label="Charges indirectes (véhicules possédés)" value={money(s.indirect_charges, s.currency)} tone="bg-slate-500/10 text-slate-600" />
            <Kpi icon={TrendingDown} label="Coût de sous-utilisation" value={money(s.under_utilisation_cost, s.currency)}
                 sub="Charges fixes non absorbées par les courses" tone="bg-rose-500/10 text-rose-600" />
          </div>
          {s.legacy_to_reconcile.count > 0 && (
            <p className="flex items-center gap-1.5 rounded-lg border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-700">
              <AlertTriangle className="h-3.5 w-3.5" />
              {s.legacy_to_reconcile.count} dépense(s) historique(s) « carburant / maintenance / assurance » à reprendre
              ({money(s.legacy_to_reconcile.amount)}) : exclues des coûts pour ne pas les compter deux fois.
            </p>
          )}
        </>
      )}

      <Card>
        <CardHeader><CardTitle>Véhicules — charges fixes et sous-utilisation</CardTitle></CardHeader>
        <CardBody className="p-0">
          {vehicles.isLoading ? (
            <div className="flex justify-center py-10"><Spinner /></div>
          ) : !vehicles.data?.results.length ? (
            <EmptyState title="Aucun coût véhicule sur la période" />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-faint">
                    <th className="px-4 py-2.5 font-medium">Véhicule</th>
                    <th className="px-4 py-2.5 text-right font-medium">Coût total</th>
                    <th className="px-4 py-2.5 text-right font-medium">Coût fixe</th>
                    <th className="px-4 py-2.5 text-right font-medium">Absorbé</th>
                    <th className="px-4 py-2.5 text-right font-medium">Sous-utilisation</th>
                    <th className="px-4 py-2.5 text-right font-medium">Utilisation</th>
                    <th className="px-4 py-2.5 text-right font-medium">/ km</th>
                    <th className="px-4 py-2.5 text-right font-medium">/ course</th>
                    <th className="px-4 py-2.5 font-medium">Manque</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {vehicles.data.results.map((r) => (
                    <tr key={r.vehicle}>
                      <td className="px-4 py-2.5 font-medium text-ink">{r.registration}<span className="block text-[11px] font-normal text-faint">{r.subsidiary_name}</span></td>
                      <td className="px-4 py-2.5 text-right">{money(r.total_cost)}</td>
                      <td className="px-4 py-2.5 text-right">{money(r.fixed_cost)}</td>
                      <td className="px-4 py-2.5 text-right">{money(r.absorbed_cost)}</td>
                      <td className="px-4 py-2.5 text-right font-semibold text-rose-600">{money(r.under_utilisation_cost)}</td>
                      <td className="px-4 py-2.5 text-right">{pct(r.utilisation_rate)}<span className="block text-[11px] text-faint">{formatNumber(r.used_km ?? 0)} / {formatNumber(r.normative_km ?? 0)} km</span></td>
                      <td className="px-4 py-2.5 text-right">{money(r.cost_per_km)}</td>
                      <td className="px-4 py-2.5 text-right">{money(r.cost_per_trip)}</td>
                      <td className="px-4 py-2.5 text-[11px] text-faint">{r.missing.map((m) => MISSING[m] ?? m).join(", ") || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardBody>
      </Card>

      <Card>
        <CardHeader><CardTitle>Courses clôturées — coût réel vs barème</CardTitle></CardHeader>
        <CardBody className="p-0">
          {trips.isLoading ? (
            <div className="flex justify-center py-10"><Spinner /></div>
          ) : !trips.data?.results.length ? (
            <EmptyState title="Aucune course clôturée sur la période" />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-faint">
                    <th className="px-4 py-2.5 font-medium">Course</th>
                    <th className="px-4 py-2.5 text-right font-medium">Coût réel</th>
                    <th className="px-4 py-2.5 text-right font-medium">Barème</th>
                    <th className="px-4 py-2.5 text-right font-medium">Écart</th>
                    <th className="px-4 py-2.5 text-right font-medium">/ km</th>
                    <th className="px-4 py-2.5 text-right font-medium">/ passager</th>
                    <th className="px-4 py-2.5 font-medium">État</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {trips.data.results.map((t) => (
                    <tr key={t.trip}>
                      <td className="px-4 py-2.5"><span className="font-medium text-ink">{t.destination}</span>
                        <span className="block text-[11px] text-faint">{t.vehicle ?? "—"} · {t.departure ? formatDate(t.departure) : "—"} · {t.distance_km ? `${formatNumber(t.distance_km)} km` : "distance inconnue"}</span></td>
                      <td className="px-4 py-2.5 text-right font-medium">{money(t.full_cost)}
                        <span className="block text-[11px] font-normal text-faint">direct {money(t.total_direct)} · indirect {money(t.total_indirect)}</span></td>
                      <td className="px-4 py-2.5 text-right">{money(t.tariff.value)}</td>
                      <td className={cn("px-4 py-2.5 text-right font-medium",
                        t.gap == null ? "text-faint" : Number(t.gap) >= 0 ? "text-emerald-600" : "text-rose-600")}>{money(t.gap)}</td>
                      <td className="px-4 py-2.5 text-right">{money(t.cost_per_km)}</td>
                      <td className="px-4 py-2.5 text-right">{money(t.cost_per_passenger)}</td>
                      <td className="px-4 py-2.5 text-xs text-muted">{STATUS[t.status]}
                        {t.missing.length > 0 && <span className="block text-[11px] text-amber-600">inconnu : {t.missing.map((m) => MISSING[m] ?? m).join(", ")}</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardBody>
      </Card>
      <p className="text-[11px] text-faint">
        Coût réel : énergie, péages, stationnement, dépenses directes (figés à la clôture de la course), puis
        assurance, amortissement, maintenance, pneumatiques et charges fixes imputés à la clôture du mois au
        prorata des km, face à la capacité normative du véhicule. « — » signifie inconnu, jamais gratuit. Écart
        = valeur au barème − coût réel.
      </p>
    </div>
  );
}
