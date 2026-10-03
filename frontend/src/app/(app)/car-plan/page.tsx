"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { CarFront } from "lucide-react";

import { AssignmentDetail } from "@/components/carplan/AssignmentDetail";
import { AssignmentsPanel } from "@/components/carplan/AssignmentsPanel";
import { DashboardPanel } from "@/components/carplan/DashboardPanel";
import { GpsAccessPanel } from "@/components/carplan/GpsAccessPanel";
import { MileageFollowupPanel } from "@/components/carplan/MileageFollowupPanel";
import { ReleasesPanel } from "@/components/carplan/ReleasesPanel";
import { RequestsIncidentsPanel } from "@/components/carplan/RequestsIncidentsPanel";
import { VehiclesPanel } from "@/components/carplan/VehiclesPanel";
import { Tabs } from "@/components/Tabs";
import { EmptyState, Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth";
import { canCarPlan } from "@/lib/carplan";

/** Car Plan — gestion des véhicules de fonction et de service attribués.
 *
 *  `?assignment=<id>` (liens des notifications) ouvre directement le détail de l'attribution.
 *  Les boutons suivent les permissions `carplan.*` ; l'API reste la barrière. */
export default function CarPlanPage() {
  return (
    <Suspense fallback={<div className="flex justify-center py-16"><Spinner className="h-7 w-7" /></div>}>
      <CarPlanContent />
    </Suspense>
  );
}

function CarPlanContent() {
  const { me } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const fromUrl = params.get("assignment");
  const [selected, setSelected] = useState<string | null>(fromUrl);

  // Suit l'URL (notification ouverte alors que la page l'est déjà).
  useEffect(() => { setSelected(fromUrl); }, [fromUrl]);

  const open = useCallback((id: string) => {
    setSelected(id);
    router.replace(`${pathname}?assignment=${encodeURIComponent(id)}`, { scroll: false });
  }, [pathname, router]);

  const close = useCallback(() => {
    setSelected(null);
    router.replace(pathname, { scroll: false });
  }, [pathname, router]);

  if (!canCarPlan(me, "view_carplan")) {
    return (
      <div className="space-y-4">
        <EmptyState title="Accès réservé aux gestionnaires Car Plan"
                    hint="La gestion des véhicules de fonction et de service attribués demande le droit « Consulter les attributions Car Plan »." />
        {me?.car_plan?.has_vehicle && (
          <p className="text-center text-sm">
            <Link href="/my-vehicle" className="font-medium text-brand-600 hover:underline">Accéder à mon véhicule</Link>
          </p>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <div className="flex items-center gap-3">
        <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-brand-500/10 text-brand-600">
          <CarFront className="h-5 w-5" />
        </span>
        <div>
          <h2 className="text-lg font-semibold text-ink">Car Plan</h2>
          <p className="text-xs text-muted">Véhicules de fonction et de service attribués : demandes, remises, suivi, restitutions et coûts.</p>
        </div>
      </div>

      <Tabs
        initialKey={fromUrl ? "assignments" : "dashboard"}
        items={[
          { key: "dashboard", label: "Tableau de bord", content: <DashboardPanel onOpen={open} /> },
          { key: "assignments", label: "Attributions", content: <AssignmentsPanel onOpen={open} /> },
          { key: "mileage", label: "Relevés & entretien", content: <MileageFollowupPanel onOpen={open} /> },
          { key: "vehicles", label: "Véhicules", content: <VehiclesPanel /> },
          { key: "requests", label: "Demandes & incidents", content: <RequestsIncidentsPanel onOpen={open} /> },
          { key: "releases", label: "Mises à disposition", content: <ReleasesPanel onOpen={open} /> },
          ...(canCarPlan(me, "view_carplan_gps")
            ? [{ key: "gps", label: "Accès GPS exceptionnels", content: <GpsAccessPanel onOpen={open} /> }]
            : []),
        ]}
      />

      {selected && <AssignmentDetail key={selected} id={selected} onClose={close} onOpen={open} />}
    </div>
  );
}
