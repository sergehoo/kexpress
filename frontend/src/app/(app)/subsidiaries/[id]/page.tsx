"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import {
  AlertTriangle, ArrowLeft, Building2, Car, ClipboardList, Fuel, Mail, MapPin,
  Phone, Receipt, Route as RouteIcon, ShieldCheck, Users, Wrench,
} from "lucide-react";

import { Card, CardBody, CardHeader, CardTitle, EmptyState, Spinner } from "@/components/ui";
import { StatChips } from "@/components/StatChips";
import { useSubsidiary, useSubsidiaryStats } from "@/lib/queries";
import { cn } from "@/lib/utils";

const fmt = (n: number) => n.toLocaleString("fr-FR");

export default function SubsidiaryDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { data: sub, isLoading, isError } = useSubsidiary(id);
  const { data: stats, isLoading: statsLoading } = useSubsidiaryStats(id);

  if (isLoading) return <div className="flex justify-center py-20"><Spinner className="h-8 w-8" /></div>;
  if (isError || !sub) {
    return (
      <div className="space-y-4">
        <BackLink />
        <Card><CardBody><EmptyState title="Filiale introuvable" /></CardBody></Card>
      </div>
    );
  }

  const v = stats?.vehicles;
  const r = stats?.reservations;
  const t = stats?.trips;
  const costs = stats?.costs;

  return (
    <div className="space-y-5">
      <BackLink />

      {/* En-tête */}
      <Card>
        <CardBody className="flex flex-wrap items-center gap-4">
          <span className="flex h-14 w-14 items-center justify-center rounded-2xl bg-brand-500/10 text-brand-600"><Building2 className="h-7 w-7" /></span>
          <div className="min-w-0">
            <h1 className="text-lg font-semibold text-ink">{sub.name}</h1>
            <p className="text-sm text-muted">{sub.company_name}</p>
            <div className="mt-1 flex flex-wrap items-center gap-3 text-xs text-muted">
              <span className="rounded-md bg-surface2 px-2 py-0.5 font-mono font-medium text-ink">{sub.code}</span>
              {sub.city && <span className="flex items-center gap-1"><MapPin className="h-3.5 w-3.5" /> {sub.city}</span>}
              {sub.email && <span className="flex items-center gap-1"><Mail className="h-3.5 w-3.5" /> {sub.email}</span>}
              {sub.phone && <span className="flex items-center gap-1"><Phone className="h-3.5 w-3.5" /> {sub.phone}</span>}
            </div>
          </div>
          <span className={cn("ml-auto rounded-full px-3 py-1 text-xs font-medium",
            sub.is_active ? "bg-emerald-500/10 text-emerald-600" : "bg-slate-500/10 text-slate-500")}>
            {sub.is_active ? "Active" : "Inactive"}
          </span>
        </CardBody>
      </Card>

      {statsLoading || !stats ? (
        <div className="flex justify-center py-12"><Spinner className="h-7 w-7" /></div>
      ) : (
        <>
          {/* KPIs clés */}
          <StatChips stats={[
            { label: "Véhicules", value: v!.total, icon: Car, tone: "bg-sky-500/10 text-sky-600", sub: `${v!.available} disponibles` },
            { label: "Chauffeurs", value: stats.drivers.total, icon: Users, tone: "bg-violet-500/10 text-violet-600", sub: `${stats.drivers.available} dispo` },
            { label: "Courses en cours", value: t!.in_progress, icon: RouteIcon, tone: "bg-emerald-500/10 text-emerald-600", sub: `${fmt(t!.distance_km)} km cumulés` },
            { label: "Demandes (mois)", value: r!.month, icon: ClipboardList, tone: "bg-amber-500/10 text-amber-600", sub: `${r!.pending} en attente` },
          ]} />

          <div className="grid gap-4 lg:grid-cols-2">
            {/* Parc par statut */}
            <Card>
              <CardHeader><CardTitle><span className="inline-flex items-center gap-2"><Car className="h-4 w-4 text-brand-500" /> Parc de véhicules</span></CardTitle></CardHeader>
              <CardBody>
                <BarRow label="Disponibles" value={v!.available} total={v!.total} tone="bg-emerald-500" />
                <BarRow label="En course" value={v!.on_trip} total={v!.total} tone="bg-sky-500" />
                <BarRow label="En maintenance" value={v!.maintenance} total={v!.total} tone="bg-amber-500" />
                <BarRow label="Hors service" value={v!.out_of_service} total={v!.total} tone="bg-rose-500" />
              </CardBody>
            </Card>

            {/* Réservations */}
            <Card>
              <CardHeader><CardTitle><span className="inline-flex items-center gap-2"><ClipboardList className="h-4 w-4 text-brand-500" /> Réservations (ce mois)</span></CardTitle></CardHeader>
              <CardBody>
                <Line label="Aujourd'hui" value={r!.today} />
                <Line label="Créées ce mois" value={r!.month} />
                <Line label="Validées" value={r!.validated} tone="text-emerald-600" />
                <Line label="En attente" value={r!.pending} tone="text-amber-600" />
                <Line label="Rejetées" value={r!.rejected} tone="text-rose-600" />
              </CardBody>
            </Card>

            {/* Courses */}
            <Card>
              <CardHeader><CardTitle><span className="inline-flex items-center gap-2"><RouteIcon className="h-4 w-4 text-brand-500" /> Courses</span></CardTitle></CardHeader>
              <CardBody>
                <Line label="En cours" value={t!.in_progress} tone="text-sky-600" />
                <Line label="Terminées" value={t!.completed} tone="text-emerald-600" />
                <Line label="Total" value={t!.total} />
                <Line label="Distance cumulée" value={`${fmt(t!.distance_km)} km`} />
              </CardBody>
            </Card>

            {/* Coûts (ce mois) — gated */}
            <Card>
              <CardHeader><CardTitle><span className="inline-flex items-center gap-2"><Receipt className="h-4 w-4 text-brand-500" /> Coûts (ce mois)</span></CardTitle></CardHeader>
              <CardBody>
                {costs!.can_see_costs ? (
                  <>
                    <Line label="Carburant" value={`${fmt(costs!.fuel ?? 0)} FCFA`} icon={Fuel} />
                    <Line label="Maintenance" value={`${fmt(costs!.maintenance ?? 0)} FCFA`} icon={Wrench} />
                    <Line label="Autres dépenses" value={`${fmt(costs!.expenses ?? 0)} FCFA`} icon={Receipt} />
                    <div className="mt-2 flex items-center justify-between border-t border-line pt-2">
                      <span className="text-sm font-semibold text-ink">Total</span>
                      <span className="text-base font-bold text-brand-600">{fmt(costs!.total ?? 0)} FCFA</span>
                    </div>
                    <p className="mt-2 text-[11px] text-faint">Carburant consommé : {fmt(costs!.fuel_liters)} L</p>
                  </>
                ) : (
                  <>
                    <Line label="Carburant consommé" value={`${fmt(costs!.fuel_liters)} L`} icon={Fuel} />
                    <p className="mt-2 rounded-lg bg-surface2 px-3 py-2 text-[11px] text-muted">
                      Le détail financier (FCFA) est réservé aux gestionnaires de flotte.
                    </p>
                  </>
                )}
              </CardBody>
            </Card>
          </div>

          {/* Alertes conformité */}
          <Card>
            <CardHeader><CardTitle><span className="inline-flex items-center gap-2"><AlertTriangle className="h-4 w-4 text-amber-500" /> Alertes & conformité</span></CardTitle></CardHeader>
            <CardBody>
              {stats.alerts.immobilized === 0 && stats.alerts.insurance_expiring === 0 && stats.alerts.inspection_expiring === 0 ? (
                <p className="flex items-center gap-2 text-sm text-emerald-600"><ShieldCheck className="h-4 w-4" /> Aucune alerte sur cette filiale.</p>
              ) : (
                <div className="grid gap-2 sm:grid-cols-3">
                  <Alert n={stats.alerts.immobilized} label="Véhicule(s) immobilisé(s)" />
                  <Alert n={stats.alerts.insurance_expiring} label="Assurance(s) à échéance (30 j)" />
                  <Alert n={stats.alerts.inspection_expiring} label="Visite(s) technique(s) à échéance" />
                </div>
              )}
            </CardBody>
          </Card>
        </>
      )}
    </div>
  );
}

function BackLink() {
  return (
    <Link href="/subsidiaries" className="inline-flex items-center gap-1.5 text-sm text-muted hover:text-ink">
      <ArrowLeft className="h-4 w-4" /> Toutes les filiales
    </Link>
  );
}

function Line({ label, value, tone, icon: Icon }: { label: string; value: string | number; tone?: string; icon?: React.ElementType }) {
  return (
    <div className="flex items-center justify-between border-b border-line py-2 last:border-0">
      <span className="flex items-center gap-1.5 text-sm text-muted">{Icon && <Icon className="h-3.5 w-3.5" />}{label}</span>
      <span className={cn("text-sm font-semibold", tone ?? "text-ink")}>{value}</span>
    </div>
  );
}

function BarRow({ label, value, total, tone }: { label: string; value: number; total: number; tone: string }) {
  const pct = total > 0 ? Math.round((value / total) * 100) : 0;
  return (
    <div className="py-1.5">
      <div className="flex items-center justify-between text-sm">
        <span className="text-muted">{label}</span>
        <span className="font-semibold text-ink">{value}</span>
      </div>
      <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-surface2">
        <div className={cn("h-full rounded-full", tone)} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

function Alert({ n, label }: { n: number; label: string }) {
  const active = n > 0;
  return (
    <div className={cn("rounded-lg border p-3 text-center", active ? "border-amber-500/30 bg-amber-500/5" : "border-line bg-surface2")}>
      <p className={cn("text-xl font-bold", active ? "text-amber-600" : "text-faint")}>{n}</p>
      <p className="mt-0.5 text-[11px] text-muted">{label}</p>
    </div>
  );
}
