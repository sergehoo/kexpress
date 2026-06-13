"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowLeft,
  Building2,
  CheckCircle2,
  Fuel,
  Gauge,
  IdCard,
  Mail,
  Phone,
  Route,
  Star,
} from "lucide-react";

import { Button, Card, CardBody, CardHeader, CardTitle, EmptyState, Spinner } from "@/components/ui";
import { StatusBadge } from "@/components/StatusBadge";
import { StatChips } from "@/components/StatChips";
import { useTrips } from "@/lib/queries";
import { api } from "@/lib/api";
import type { Driver } from "@/lib/types";
import { cn, formatDate, formatNumber } from "@/lib/utils";

function useDriver(id?: string | null) {
  return useQuery({
    queryKey: ["driver", id],
    enabled: !!id,
    queryFn: async () => {
      const { data } = await api.get<Driver>(`/drivers/${id}/`);
      return data;
    },
  });
}

export default function DriverDetailPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { data: d, isLoading, isError } = useDriver(id);
  const trips = useTrips({ driver: id, page_size: "100" });

  if (isLoading) {
    return <div className="flex justify-center py-20"><Spinner className="h-7 w-7" /></div>;
  }
  if (isError || !d) {
    return (
      <Card>
        <CardBody>
          <EmptyState title="Chauffeur introuvable" />
          <div className="flex justify-center pt-2">
            <Button variant="secondary" onClick={() => router.push("/drivers")}>
              <ArrowLeft className="h-4 w-4" /> Retour aux chauffeurs
            </Button>
          </div>
        </CardBody>
      </Card>
    );
  }

  const list = trips.data?.results ?? [];
  const done = list.filter((t) => ["returned", "closed"].includes(t.status));
  const totalKm = done.reduce((s, t) => s + Number(t.distance_km ?? 0), 0);
  const totalFuel = done.reduce((s, t) => s + Number(t.fuel_consumed ?? 0), 0);
  const rate = totalKm > 0 && totalFuel > 0 ? ((totalFuel / totalKm) * 100).toFixed(1) : null;
  const licenseDays = d.license_expiry
    ? Math.floor((new Date(d.license_expiry).getTime() - Date.now()) / 86_400_000)
    : null;

  return (
    <div className="mx-auto max-w-5xl space-y-4">
      {/* En-tête */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <Link href="/drivers" className="inline-flex items-center gap-1 text-xs font-medium text-muted hover:text-ink">
            <ArrowLeft className="h-3.5 w-3.5" /> Chauffeurs
          </Link>
          <h1 className="mt-1 text-xl font-bold text-ink">{d.full_name}</h1>
          <p className="text-sm text-muted">{d.subsidiary_name}</p>
        </div>
        <span className={cn(
          "inline-flex items-center rounded-full px-3 py-1 text-xs font-medium ring-1 ring-inset",
          d.is_available
            ? "bg-emerald-500/10 text-emerald-600 ring-emerald-500/20"
            : "bg-slate-500/10 text-slate-500 ring-slate-500/20",
        )}>
          {d.is_available ? "Disponible" : "Indisponible"}
        </span>
      </div>

      {/* Alerte permis */}
      {licenseDays != null && licenseDays <= 30 && (
        <div className={cn(
          "flex items-center gap-2 rounded-xl border px-4 py-2.5 text-sm",
          licenseDays < 0 ? "border-rose-500/30 bg-rose-500/5 text-rose-600" : "border-amber-500/30 bg-amber-500/5 text-amber-600",
        )}>
          <AlertTriangle className="h-4 w-4 shrink-0" />
          {licenseDays < 0
            ? `Permis expiré depuis ${Math.abs(licenseDays)} jour(s) — chauffeur à ne plus affecter.`
            : `Permis expire dans ${licenseDays} jour(s) (${formatDate(d.license_expiry)}).`}
        </div>
      )}

      {/* Statistiques d'activité réelles */}
      <StatChips
        stats={[
          { label: "Courses effectuées", value: done.length, icon: CheckCircle2, tone: "bg-emerald-500/10 text-emerald-600" },
          { label: "Km parcourus", value: formatNumber(totalKm, "km"), icon: Route, tone: "bg-sky-500/10 text-sky-600" },
          { label: "Carburant consommé", value: `${formatNumber(totalFuel)} L`, icon: Fuel, tone: "bg-orange-500/10 text-orange-600" },
          { label: "Conso. moyenne", value: rate ? `${rate} L/100km` : "—", icon: Gauge, tone: "bg-violet-500/10 text-violet-600" },
          { label: "Note", value: d.rating ?? "—", icon: Star, tone: "bg-amber-500/10 text-amber-600" },
        ]}
      />

      <div className="grid gap-4 lg:grid-cols-2">
        {/* Coordonnées & permis */}
        <Card>
          <CardHeader><CardTitle>Coordonnées & permis</CardTitle></CardHeader>
          <CardBody className="space-y-3">
            <Info icon={Phone} label="Téléphone" value={d.phone || "—"} />
            <Info icon={Mail} label="Email" value={d.email || "—"} />
            <Info icon={IdCard} label="Permis" value={`${d.license_number || "—"}${d.license_category ? ` · catégorie ${d.license_category}` : ""}`} />
            <Info
              icon={IdCard}
              label="Expiration du permis"
              value={d.license_expiry ? `${formatDate(d.license_expiry)}${licenseDays != null ? ` (J${licenseDays >= 0 ? "-" : "+"}${Math.abs(licenseDays)})` : ""}` : "non renseignée"}
            />
            <Info icon={Building2} label="Filiale" value={d.subsidiary_name} />
          </CardBody>
        </Card>

        {/* Historique des courses */}
        <Card>
          <CardHeader><CardTitle>Courses récentes</CardTitle></CardHeader>
          <CardBody className="p-0">
            {list.length === 0 ? (
              <div className="p-4"><EmptyState title="Aucune course" /></div>
            ) : (
              <ul className="max-h-96 divide-y divide-line overflow-y-auto">
                {list.slice(0, 15).map((t) => (
                  <li key={t.id} className="flex items-center gap-3 px-5 py-2.5 text-sm">
                    <div className="min-w-0 flex-1">
                      <Link href={`/trips/${t.id}`} className="block truncate font-medium text-ink hover:text-brand-600 hover:underline">
                        {t.destination}
                      </Link>
                      <p className="text-[11px] text-muted">
                        {t.vehicle_registration} · {formatDate(t.actual_departure ?? t.created_at, true)}
                        {t.distance_km ? ` · ${formatNumber(t.distance_km, "km")}` : ""}
                      </p>
                    </div>
                    <StatusBadge code={t.status} label={t.status_display} />
                  </li>
                ))}
              </ul>
            )}
          </CardBody>
        </Card>
      </div>
    </div>
  );
}

function Info({ icon: Icon, label, value }: { icon: React.ElementType; label: string; value: string }) {
  return (
    <div className="flex items-start gap-2.5">
      <Icon className="mt-0.5 h-4 w-4 shrink-0 text-faint" />
      <div className="min-w-0">
        <p className="text-[11px] uppercase tracking-wide text-faint">{label}</p>
        <p className="text-sm font-medium text-ink">{value}</p>
      </div>
    </div>
  );
}
