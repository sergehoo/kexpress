"use client";

import { useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";

import { Sidebar } from "@/components/Sidebar";
import { Topbar } from "@/components/Topbar";
import { KBot } from "@/components/KBot";
import { OfflineSync } from "@/components/OfflineSync";
import { Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth";
import { canAccess, homeFor } from "@/lib/rbac";

const TITLES: Record<string, string> = {
  "/dashboard": "Tableau de bord",
  "/fleet-control": "Centre de contrôle flotte",
  "/map": "Carte temps réel",
  "/reservations": "Réservations",
  "/planning-vehicles": "Planning véhicules",
  "/planning-drivers": "Planning chauffeurs",
  "/trips": "Courses",
  "/vehicles": "Véhicules",
  "/drivers": "Chauffeurs",
  "/maintenance": "Maintenance",
  "/fuel": "Carburant",
  "/expenses": "Dépenses",
  "/reports": "Rapports",
  "/subsidiaries": "Filiales",
  "/employees": "Employés",
  "/incidents": "Incidents",
  "/alerts": "Alertes",
  "/notifications": "Centre de notifications",
  "/kbot": "K-BOT",
  "/audit": "Journal d'audit",
  "/settings": "Paramètres",
};

export default function AppLayout({ children }: { children: React.ReactNode }) {
  const { me, loading } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const [menuOpen, setMenuOpen] = useState(false);

  useEffect(() => {
    if (!loading && !me) router.replace("/login");
  }, [me, loading, router]);

  // Vue organisée par rôle : une page hors périmètre redirige vers
  // l'accueil du rôle (les données restent de toute façon protégées par l'API).
  const allowed = !me || canAccess(me.role, pathname);
  useEffect(() => {
    if (me && !allowed) router.replace(homeFor(me.role));
  }, [me, allowed, router]);

  if (loading || !me || !allowed) {
    return (
      <div className="flex h-screen items-center justify-center">
        <Spinner className="h-8 w-8" />
      </div>
    );
  }

  const title =
    Object.entries(TITLES).find(([k]) => pathname.startsWith(k))?.[1] ?? "Kaydan Express";

  return (
    <div className="min-h-screen">
      <Sidebar open={menuOpen} onClose={() => setMenuOpen(false)} />
      <div className="lg:pl-64">
        <Topbar title={title} onMenu={() => setMenuOpen(true)} />
        <main className="mx-auto max-w-7xl px-4 py-6 lg:px-8">{children}</main>
      </div>
      <KBot />
      <OfflineSync />
    </div>
  );
}
