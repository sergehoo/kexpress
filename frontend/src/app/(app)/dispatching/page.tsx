"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import {
  AlertTriangle,
  CalendarClock,
  Car,
  Grid3x3,
  Inbox,
  Lightbulb,
  Route,
  Sparkles,
  TrendingUp,
  Users,
} from "lucide-react";

import { Button, Card, CardBody, CardHeader, CardTitle, EmptyState, Select, Spinner } from "@/components/ui";
import { StatusBadge } from "@/components/StatusBadge";
import { Modal } from "@/components/Modal";
import {
  useDecideSuggestion,
  useDispatchAnticipation,
  useDispatchBoard,
  useDispatchSuggestions,
  useGenerateSuggestions,
  useMutualisationPotential,
  type DispatchSuggestion,
} from "@/lib/queries";
import { useSubsidiaryFilter } from "@/lib/subsidiary";
import { useAuth } from "@/lib/auth";
import { canFinance } from "@/lib/rbac";
import { apiError } from "@/lib/api";
import { cn, formatDate, formatNumber } from "@/lib/utils";

type View = "unassigned" | "matrix" | "missions" | "suggestions" | "potential" | "anticipation";

const VIEWS: { key: View; label: string; icon: React.ElementType }[] = [
  { key: "unassigned", label: "À affecter", icon: Inbox },
  { key: "matrix", label: "Matrice zones", icon: Grid3x3 },
  { key: "missions", label: "Tournées", icon: Route },
  { key: "suggestions", label: "Suggestions", icon: Lightbulb },
  { key: "potential", label: "Potentiel", icon: TrendingUp },
  { key: "anticipation", label: "Anticipation", icon: CalendarClock },
];

const HORIZONS = [
  { value: "6", label: "6 h" },
  { value: "12", label: "12 h" },
  { value: "24", label: "24 h" },
  { value: "72", label: "3 jours" },
];

/** Centre de dispatching (§4).
 *
 *  Une seule requête alimente les quatre vues : changer de mode d'affichage ne relance rien
 *  et les chiffres restent cohérents entre elles. La carte interactive et le planning horaire
 *  existent déjà (`/map`, `/planning-vehicles`) — ils sont liés plutôt que dupliqués.
 *
 *  Aucune suggestion ne s'applique d'elle-même : l'acceptation exige de choisir un véhicule,
 *  et le serveur revérifie toutes les contraintes au moment de la décision (§9). */
export default function DispatchingPage() {
  const { selected } = useSubsidiaryFilter();
  const [view, setView] = useState<View>("unassigned");
  const [hours, setHours] = useState("24");
  const [toast, setToast] = useState("");

  const params = useMemo(() => {
    const next: Record<string, string> = { hours };
    if (selected) next.subsidiary = selected;
    return next;
  }, [hours, selected]);

  const { data, isLoading } = useDispatchBoard(params);
  const suggestions = useDispatchSuggestions();
  const generate = useGenerateSuggestions();

  if (isLoading) {
    return <div className="flex justify-center py-20"><Spinner className="h-7 w-7" /></div>;
  }
  if (!data) {
    return (
      <Card><CardBody>
        <EmptyState title="Centre de dispatching indisponible" hint="Vérifiez la connexion au serveur." />
      </CardBody></Card>
    );
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-ink">Centre de dispatching</h2>
          <p className="text-sm text-muted">
            {formatDate(data.window.start, true)} → {formatDate(data.window.end, true)}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Select value={hours} onChange={(e) => setHours(e.target.value)} className="w-28">
            {HORIZONS.map((h) => <option key={h.value} value={h.value}>{h.label}</option>)}
          </Select>
          <Button
            variant="secondary"
            disabled={generate.isPending}
            onClick={() => generate.mutate(undefined, {
              onSuccess: () => setView("suggestions"),
              onError: (e) => setToast(apiError(e)),
            })}
          >
            <Sparkles className="h-4 w-4" /> Analyser les regroupements
          </Button>
        </div>
      </div>

      {toast && (
        <div className="flex items-center justify-between rounded-lg bg-rose-500/10 px-4 py-2 text-sm text-rose-600">
          {toast}
          <button onClick={() => setToast("")} className="text-rose-400 hover:text-rose-600">✕</button>
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Tile icon={Route} label="Courses" value={data.totals.trips} tone="bg-sky-500/10 text-sky-600" />
        <Tile icon={Inbox} label="À affecter" value={data.totals.unassigned}
              tone={data.totals.unassigned > 0 ? "bg-amber-500/10 text-amber-600" : "bg-surface2 text-muted"} />
        <Tile icon={Users} label="Passagers" value={data.totals.passengers} tone="bg-violet-500/10 text-violet-600" />
        <Tile icon={Lightbulb} label="Suggestions" value={data.pending_suggestions}
              tone={data.pending_suggestions > 0 ? "bg-emerald-500/10 text-emerald-600" : "bg-surface2 text-muted"} />
      </div>

      <div className="flex w-fit flex-wrap rounded-lg border border-line bg-surface p-0.5">
        {VIEWS.map(({ key, label, icon: Icon }) => (
          <button
            key={key}
            onClick={() => setView(key)}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition-colors",
              view === key ? "bg-brand-600 text-white" : "text-muted hover:bg-surface2",
            )}
          >
            <Icon className="h-3.5 w-3.5" /> {label}
          </button>
        ))}
      </div>

      {view === "unassigned" && <UnassignedView data={data} />}
      {view === "matrix" && <MatrixView data={data} />}
      {view === "missions" && <MissionsView data={data} />}
      {view === "potential" && <PotentialView />}
      {view === "anticipation" && <AnticipationView />}
      {view === "suggestions" && (
        <SuggestionsView
          rows={suggestions.data ?? []}
          loading={suggestions.isLoading}
          vehicles={data.available_vehicles}
          onError={setToast}
        />
      )}

      <p className="text-[11px] text-faint">
        Carte temps réel : <Link href="/map" className="text-brand-600 hover:underline">/map</Link>
        {" · "}planning horaire : <Link href="/planning-vehicles" className="text-brand-600 hover:underline">/planning-vehicles</Link>
      </p>
    </div>
  );
}

/** Potentiel de mutualisation (simulation contrefactuelle).
 *
 *  Répond à « qu'aurions-nous économisé ? » — l'argument qui fait accepter le partage de
 *  véhicule aux filiales réticentes. Les hypothèses sont affichées AVEC le chiffre : sans
 *  elles, une estimation serait lue comme une mesure et se retournerait contre le module. */
function PotentialView() {
  const { data, isLoading } = useMutualisationPotential({ period: "month" });

  if (isLoading) return <div className="flex justify-center py-12"><Spinner className="h-6 w-6" /></div>;
  if (!data) return null;
  if (data.trips_examined === 0) {
    return (
      <Card><CardBody>
        <EmptyState title="Aucune course terminée à analyser"
                    hint="Le potentiel se calcule sur les courses déjà réalisées seules." />
      </CardBody></Card>
    );
  }

  const energy = Object.entries(data.energy_avoided)
    .map(([unit, value]) => `${formatNumber(value)} ${unit}`)
    .join(" + ") || "—";

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>Si ces courses avaient été mutualisées ({data.period})</CardTitle>
        </CardHeader>
        <CardBody className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-4">
            <Tile icon={Route} label="Kilomètres évités"
                  value={Math.round(data.km_avoided)} tone="bg-emerald-500/10 text-emerald-600" />
            <Tile icon={Lightbulb} label="Regroupements manqués"
                  value={data.groupings} tone="bg-amber-500/10 text-amber-600" />
            <Tile icon={Users} label="Courses concernées"
                  value={data.trips_groupable} tone="bg-sky-500/10 text-sky-600" />
            <Tile icon={TrendingUp} label="CO₂ évité (kg)"
                  value={Math.round(data.co2_avoided_kg)} tone="bg-violet-500/10 text-violet-600" />
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="rounded-lg border border-line bg-surface2/40 p-3">
              <p className="text-[11px] uppercase tracking-wide text-faint">Énergie évitée</p>
              <p className="text-sm font-semibold text-ink">{energy}</p>
            </div>
            <div className="rounded-lg border border-line bg-surface2/40 p-3">
              <p className="text-[11px] uppercase tracking-wide text-faint">Coût évité</p>
              <p className="text-sm font-semibold text-ink">
                {data.cost_avoided > 0 ? `${formatNumber(data.cost_avoided)} XOF` : "—"}
              </p>
            </div>
          </div>
        </CardBody>
      </Card>

      <Card>
        <CardBody>
          <p className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-faint">
            <AlertTriangle className="h-3.5 w-3.5" /> Ce que cette estimation suppose
          </p>
          <ul className="space-y-1">
            {data.assumptions.map((line, index) => (
              <li key={index} className="text-xs text-muted">· {line}</li>
            ))}
          </ul>
        </CardBody>
      </Card>
    </div>
  );
}

/** Dispatching anticipatif — demandes récurrentes de l'historique.
 *
 *  Sépare les motifs attendus cette semaine en deux : ceux qu'une réservation couvre déjà,
 *  et ceux « à anticiper » — la seule liste qui appelle une action. Rien n'est réservé
 *  automatiquement (§9) : le motif informe, le dispatcher décide. */
function AnticipationView() {
  const { data, isLoading } = useDispatchAnticipation();

  if (isLoading) return <div className="flex justify-center py-12"><Spinner className="h-6 w-6" /></div>;
  if (!data) return null;

  const toAnticipate = data.patterns.filter((p) => !p.covered);
  const covered = data.patterns.filter((p) => p.covered);

  if (data.patterns.length === 0) {
    return (
      <Card><CardBody>
        <EmptyState
          title="Aucune demande récurrente détectée"
          hint={`Analyse des ${data.window.weeks} dernières semaines : un motif exige au moins 3 occurrences réparties sur la moitié des semaines.`}
        />
      </CardBody></Card>
    );
  }

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>
            Demandes récurrentes attendues sous 7 jours
            {toAnticipate.length > 0 && (
              <span className="ml-2 rounded-full bg-amber-500/10 px-2 py-0.5 text-xs font-semibold text-amber-600">
                {toAnticipate.length} sans réservation
              </span>
            )}
          </CardTitle>
        </CardHeader>
        <CardBody className="p-0">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-faint">
                  <th className="px-5 py-3 font-medium">Trajet</th>
                  <th className="px-5 py-3 font-medium">Habitude</th>
                  <th className="px-5 py-3 font-medium">Régularité</th>
                  <th className="px-5 py-3 font-medium">Passagers (moy.)</th>
                  <th className="px-5 py-3 font-medium">Prochaine occurrence</th>
                  <th className="px-5 py-3 font-medium">Réservation</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {[...toAnticipate, ...covered].map((p, index) => (
                  <tr key={index} className={cn("hover:bg-surface2", !p.covered && "bg-amber-500/[0.04]")}>
                    <td className="px-5 py-3">
                      <p className="font-medium text-ink">{p.origin || "—"} → {p.destination}</p>
                    </td>
                    <td className="px-5 py-3 text-muted">
                      {p.weekday_label} {p.time}
                      <span className="ml-1 text-[11px] text-faint">
                        ({p.weeks_seen}/{p.weeks_observed} sem.)
                      </span>
                    </td>
                    <td className="px-5 py-3 text-muted">{Math.round(p.regularity * 100)} %</td>
                    <td className="px-5 py-3 text-muted">{p.avg_passengers}</td>
                    <td className="px-5 py-3 text-muted">{formatDate(p.next_expected)}</td>
                    <td className="px-5 py-3">
                      {p.covered ? (
                        <span className="rounded-full bg-emerald-500/10 px-2 py-0.5 text-xs font-medium text-emerald-600">
                          couverte
                        </span>
                      ) : (
                        <span className="rounded-full bg-amber-500/10 px-2 py-0.5 text-xs font-semibold text-amber-600">
                          à anticiper
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </CardBody>
      </Card>

      <Card>
        <CardBody>
          <p className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-faint">
            <AlertTriangle className="h-3.5 w-3.5" /> Ce que cette détection suppose
          </p>
          <ul className="space-y-1">
            {data.assumptions.map((line, index) => (
              <li key={index} className="text-xs text-muted">· {line}</li>
            ))}
          </ul>
        </CardBody>
      </Card>
    </div>
  );
}

function Tile({ icon: Icon, label, value, tone }: {
  icon: React.ElementType; label: string; value: number; tone: string;
}) {
  return (
    <Card>
      <CardBody className="flex items-center gap-3 py-3">
        <span className={cn("flex h-9 w-9 items-center justify-center rounded-xl", tone)}>
          <Icon className="h-4 w-4" />
        </span>
        <div>
          <p className="text-lg font-semibold leading-none text-ink">{value}</p>
          <p className="text-[11px] text-muted">{label}</p>
        </div>
      </CardBody>
    </Card>
  );
}

type BoardData = NonNullable<ReturnType<typeof useDispatchBoard>["data"]>;

/** Coût kilométrique estimé : servi par l'API aux seuls profils `view_trip_cost` (§10). */
function PricingCell({ trip }: { trip: BoardData["unassigned"][number] }) {
  const pricing = trip.pricing;
  if (!pricing || pricing.amount_per_km == null) {
    return <span className="text-faint" title="Aucun barème en vigueur à cette date">non valorisée</span>;
  }
  return (
    <span>
      <b className="text-ink">{pricing.estimated_cost != null ? `${formatNumber(pricing.estimated_cost)} ${pricing.currency}` : "—"}</b>
      <span className="block text-[11px] text-faint">{formatNumber(pricing.amount_per_km)} {pricing.currency}/km</span>
    </span>
  );
}

function UnassignedView({ data }: { data: BoardData }) {
  const { me } = useAuth();
  const withCosts = canFinance(me, "view_trip_cost");
  if (data.unassigned.length === 0) {
    return (
      <Card><CardBody>
        <EmptyState title="Aucune course en attente d'affectation"
                    hint="Toutes les courses de la période ont un véhicule." />
      </CardBody></Card>
    );
  }
  return (
    <Card>
      <CardHeader><CardTitle>Courses à affecter ({data.unassigned.length})</CardTitle></CardHeader>
      <CardBody className="p-0">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-faint">
                <th className="px-5 py-3 font-medium">Départ prévu</th>
                <th className="px-5 py-3 font-medium">Zone départ → arrivée</th>
                <th className="px-5 py-3 font-medium">Destination</th>
                <th className="px-5 py-3 font-medium">Distance</th>
                <th className="px-5 py-3 font-medium">Passagers</th>
                {withCosts && <th className="px-5 py-3 font-medium">Coût estimé</th>}
                <th className="px-5 py-3 font-medium">Filiale</th>
                <th className="px-5 py-3 font-medium text-right">Détail</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {data.unassigned.map((trip) => (
                <tr key={trip.id} className="hover:bg-surface2">
                  <td className="px-5 py-3 text-muted">
                    {trip.planned_departure_at ? formatDate(trip.planned_departure_at, true) : "—"}
                  </td>
                  <td className="px-5 py-3 text-muted">
                    {trip.origin_zone_name || "—"} → {trip.destination_zone_name || "—"}
                  </td>
                  <td className="px-5 py-3 font-medium text-ink">{trip.destination}</td>
                  <td className="px-5 py-3 text-muted">{trip.distance_km != null ? formatNumber(trip.distance_km, "km") : "—"}</td>
                  <td className="px-5 py-3 text-muted">{trip.passengers ?? "—"}</td>
                  {withCosts && <td className="px-5 py-3 text-muted"><PricingCell trip={trip} /></td>}
                  <td className="px-5 py-3 text-muted">{trip.subsidiary_name || "—"}</td>
                  <td className="px-5 py-3 text-right">
                    <Link href={`/trips/${trip.id}`} className="text-xs font-medium text-brand-600 hover:underline">
                      Voir
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </CardBody>
    </Card>
  );
}

function MatrixView({ data }: { data: BoardData }) {
  if (data.zone_matrix.length === 0) {
    return (
      <Card><CardBody>
        <EmptyState title="Aucune course sur la période" hint="Élargissez l'horizon pour voir la demande." />
      </CardBody></Card>
    );
  }
  const busiest = Math.max(...data.zone_matrix.map((c) => c.trips));
  return (
    <Card>
      <CardHeader><CardTitle>Matrice départ → destination</CardTitle></CardHeader>
      <CardBody className="p-0">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-faint">
                <th className="px-5 py-3 font-medium">Zone de départ</th>
                <th className="px-5 py-3 font-medium">Zone d&apos;arrivée</th>
                <th className="px-5 py-3 font-medium">Courses</th>
                <th className="px-5 py-3 font-medium">Passagers</th>
                <th className="px-5 py-3 font-medium text-right">À affecter</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {data.zone_matrix.map((cell) => (
                <tr key={`${cell.origin_zone_name}->${cell.destination_zone_name}`} className="hover:bg-surface2">
                  <td className="px-5 py-3 font-medium text-ink">{cell.origin_zone_name}</td>
                  <td className="px-5 py-3 text-ink">{cell.destination_zone_name}</td>
                  <td className="px-5 py-3">
                    <span className="inline-flex items-center gap-2">
                      <span className="h-1.5 rounded-full bg-brand-500"
                            style={{ width: `${Math.max(8, (cell.trips / busiest) * 80)}px` }} />
                      <span className="text-muted">{cell.trips}</span>
                    </span>
                  </td>
                  <td className="px-5 py-3 text-muted">{cell.passengers}</td>
                  <td className={cn("px-5 py-3 text-right font-medium",
                                    cell.unassigned > 0 ? "text-amber-600" : "text-faint")}>
                    {cell.unassigned}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </CardBody>
    </Card>
  );
}

function MissionsView({ data }: { data: BoardData }) {
  if (data.missions.length === 0) {
    return (
      <Card><CardBody>
        <EmptyState title="Aucune tournée sur la période"
                    hint="Analysez les regroupements pour en proposer." />
      </CardBody></Card>
    );
  }
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {data.missions.map((mission) => (
        <Card key={mission.id}>
          <CardBody className="space-y-2">
            <div className="flex items-center justify-between gap-2">
              <span className="font-semibold text-ink">{mission.code}</span>
              <StatusBadge code={mission.status} label={mission.status_display} />
            </div>
            <p className="flex items-center gap-1.5 text-sm text-muted">
              <Car className="h-3.5 w-3.5" /> {mission.vehicle_registration}
              <span className="text-faint">({mission.vehicle_capacity} pl.)</span>
            </p>
            <p className="text-xs text-muted">{mission.driver_name || "Chauffeur non affecté"}</p>
            <p className="text-xs text-faint">
              {mission.trips} course(s)
              {mission.planned_departure_at ? ` · ${formatDate(mission.planned_departure_at, true)}` : ""}
            </p>
          </CardBody>
        </Card>
      ))}
    </div>
  );
}

/** Impact d'un regroupement : sans / avec mutualisation (§10). Même formule que la
 *  simulation de potentiel : les deux écrans n'annoncent jamais deux économies différentes. */
function PoolingImpactCard({ impact }: { impact: NonNullable<DispatchSuggestion["financial_impact"]> }) {
  const money = (value: string | null) => (value != null ? `${formatNumber(value)} ${impact.currency}` : "—");
  return (
    <div className="grid gap-2 rounded-lg border border-line bg-surface2/40 p-3 text-xs sm:grid-cols-3">
      <div>
        <p className="text-[11px] uppercase tracking-wide text-faint">Sans mutualisation</p>
        <p className="font-semibold text-ink">{formatNumber(impact.km_separate, "km")}</p>
        <p className="text-muted">{money(impact.cost_separate)}</p>
      </div>
      <div>
        <p className="text-[11px] uppercase tracking-wide text-faint">Avec mutualisation</p>
        <p className="font-semibold text-ink">{formatNumber(impact.km_grouped, "km")}</p>
        <p className="text-muted">{money(impact.cost_grouped)}</p>
      </div>
      <div>
        <p className="text-[11px] uppercase tracking-wide text-faint">Distance évitée · économie théorique</p>
        <p className="font-semibold text-emerald-600">{formatNumber(impact.km_avoided, "km")}</p>
        <p className="font-semibold text-emerald-600">{money(impact.saving)}</p>
      </div>
      {impact.amount_per_km == null && (
        <p className="text-[11px] text-faint sm:col-span-3">Aucun barème en vigueur à cette date : seuls les kilomètres sont chiffrés.</p>
      )}
      {impact.approximate && (
        <p className="text-[11px] text-faint sm:col-span-3">Détour estimé à vol d&apos;oiseau (itinéraire routier indisponible), corrigé par un facteur de sinuosité.</p>
      )}
    </div>
  );
}

function SuggestionsView({ rows, loading, vehicles, onError }: {
  rows: DispatchSuggestion[];
  loading: boolean;
  vehicles: BoardData["available_vehicles"];
  onError: (message: string) => void;
}) {
  const [target, setTarget] = useState<DispatchSuggestion | null>(null);
  const decide = useDecideSuggestion();

  if (loading) return <div className="flex justify-center py-12"><Spinner className="h-6 w-6" /></div>;
  if (rows.length === 0) {
    return (
      <Card><CardBody>
        <EmptyState title="Aucune suggestion en attente"
                    hint="Lancez « Analyser les regroupements » pour en produire." />
      </CardBody></Card>
    );
  }

  return (
    <>
      <div className="space-y-3">
        {rows.map((row) => (
          <Card key={row.id}>
            <CardBody className="space-y-2.5">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-sm font-semibold text-ink">{row.kind_display}</span>
                <span className="rounded-full bg-brand-500/10 px-2.5 py-0.5 text-[11px] font-medium text-brand-600">
                  pertinence {Math.round(row.score * 100)} %
                </span>
              </div>
              {/* §20 — la proposition s'explique avec les données qui l'ont produite. */}
              <p className="text-sm text-muted">{row.rationale}</p>
              {row.financial_impact && <PoolingImpactCard impact={row.financial_impact} />}
              <div className="flex flex-wrap gap-1.5 border-t border-line pt-2.5">
                <Button size="sm" onClick={() => setTarget(row)}>Valider…</Button>
                <Button
                  size="sm" variant="ghost" disabled={decide.isPending}
                  onClick={() => decide.mutate(
                    { id: row.id, action: "reject" },
                    { onError: (e) => onError(apiError(e)) },
                  )}
                >
                  Rejeter
                </Button>
              </div>
            </CardBody>
          </Card>
        ))}
      </div>

      {target && (
        <DecisionModal
          suggestion={target}
          vehicles={vehicles}
          pending={decide.isPending}
          onClose={() => setTarget(null)}
          onConfirm={(vehicle) => decide.mutate(
            { id: target.id, action: "accept", vehicle },
            { onSuccess: () => setTarget(null), onError: (e) => onError(apiError(e)) },
          )}
        />
      )}
    </>
  );
}

function DecisionModal({ suggestion, vehicles, pending, onClose, onConfirm }: {
  suggestion: DispatchSuggestion;
  vehicles: BoardData["available_vehicles"];
  pending: boolean;
  onClose: () => void;
  onConfirm: (vehicle: string) => void;
}) {
  const required = suggestion.payload.capacity_required ?? 0;
  const eligible = vehicles.filter((v) => v.capacity >= required);
  const [vehicle, setVehicle] = useState("");

  return (
    <Modal open title="Valider le regroupement" onClose={onClose}>
      <p className="mb-3 text-sm text-muted">{suggestion.rationale}</p>
      <label className="mb-1 block text-xs font-medium text-muted">
        Véhicule (capacité ≥ {required})
      </label>
      <Select value={vehicle} onChange={(e) => setVehicle(e.target.value)}>
        <option value="">— Choisir —</option>
        {eligible.map((v) => (
          <option key={v.id} value={v.id}>
            {v.registration} · {v.label} ({v.capacity} pl.)
          </option>
        ))}
      </Select>
      {eligible.length === 0 && (
        <p className="mt-2 flex items-start gap-1.5 text-xs text-amber-600">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          Aucun véhicule disponible avec une capacité suffisante ({required} places).
        </p>
      )}
      <p className="mt-3 text-[11px] text-faint">
        La capacité, les conflits horaires et l&apos;autonomie sont revérifiés au moment de la
        validation : une proposition devenue irréalisable sera refusée.
      </p>
      <div className="flex justify-end gap-2 pt-3">
        <Button variant="secondary" onClick={onClose}>Annuler</Button>
        <Button disabled={!vehicle || pending} onClick={() => onConfirm(vehicle)}>
          Créer la tournée
        </Button>
      </div>
    </Modal>
  );
}
