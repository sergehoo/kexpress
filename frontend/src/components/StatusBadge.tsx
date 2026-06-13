import { cn } from "@/lib/utils";

type Tone = "green" | "blue" | "amber" | "red" | "slate" | "violet" | "cyan";

const toneClass: Record<Tone, string> = {
  green: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-300 ring-emerald-500/20",
  blue: "bg-sky-500/10 text-sky-600 dark:text-sky-300 ring-sky-500/20",
  amber: "bg-amber-500/10 text-amber-600 dark:text-amber-300 ring-amber-500/20",
  red: "bg-rose-500/10 text-rose-600 dark:text-rose-300 ring-rose-500/20",
  slate: "bg-slate-500/10 text-slate-500 dark:text-slate-300 ring-slate-500/20",
  violet: "bg-violet-500/10 text-violet-600 dark:text-violet-300 ring-violet-500/20",
  cyan: "bg-cyan-500/10 text-cyan-600 dark:text-cyan-300 ring-cyan-500/20",
};

// Mappage des codes de statut (véhicule / réservation / course) vers une teinte.
const STATUS_TONE: Record<string, Tone> = {
  // Véhicule
  available: "green",
  reserved: "violet",
  on_trip: "blue",
  maintenance: "amber",
  out_of_service: "red",
  unavailable: "slate",
  // Réservation
  draft: "slate",
  submitted: "blue",
  pending_manager: "amber",
  pending_fleet: "amber",
  approved: "green",
  rejected: "red",
  cancelled: "slate",
  vehicle_assigned: "violet",
  driver_assigned: "violet",
  in_progress: "blue",
  completed: "green",
  closed: "slate",
  // Course
  scheduled: "violet",
  departed: "blue",
  returned: "cyan",
  // Priorité
  low: "slate",
  normal: "blue",
  high: "amber",
  urgent: "red",
};

export function StatusBadge({
  code,
  label,
  className,
}: {
  code: string;
  label: string;
  className?: string;
}) {
  const tone = STATUS_TONE[code] ?? "slate";
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset",
        toneClass[tone],
        className,
      )}
    >
      {label}
    </span>
  );
}
