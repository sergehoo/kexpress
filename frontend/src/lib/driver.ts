import type { DriverMission } from "@/lib/types";

/** Liens de navigation externe vers la destination (apps natives mobiles). */
export function wazeUrl(lat: number, lng: number): string {
  return `https://waze.com/ul?ll=${lat},${lng}&navigate=yes`;
}

export function googleMapsUrl(lat: number, lng: number): string {
  return `https://www.google.com/maps/dir/?api=1&destination=${lat},${lng}&travelmode=driving`;
}

/** Statut opérationnel du chauffeur déduit de ses missions. */
export function driverStatus(missions: DriverMission[] | undefined): { label: string; tone: string } {
  const m = missions ?? [];
  if (m.some((x) => x.status === "in_progress" || x.status === "departed"))
    return { label: "En course", tone: "bg-sky-500/10 text-sky-600 ring-sky-500/20" };
  if (m.some((x) => x.status === "returned"))
    return { label: "Retour à clôturer", tone: "bg-amber-500/10 text-amber-600 ring-amber-500/20" };
  if (m.some((x) => x.status === "scheduled"))
    return { label: "Affecté", tone: "bg-violet-500/10 text-violet-600 ring-violet-500/20" };
  return { label: "Disponible", tone: "bg-emerald-500/10 text-emerald-600 ring-emerald-500/20" };
}

/** Mission « courante » à afficher en priorité : en cours/retour, sinon la prochaine planifiée. */
export function currentMission(missions: DriverMission[] | undefined): DriverMission | null {
  const m = missions ?? [];
  return (
    m.find((x) => x.status === "in_progress" || x.status === "departed" || x.status === "returned") ||
    m.find((x) => x.status === "scheduled") ||
    null
  );
}
