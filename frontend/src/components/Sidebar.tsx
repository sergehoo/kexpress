"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  AlertTriangle,
  Bell,
  BellRing,
  Bot,
  Building2,
  CalendarCheck,
  CalendarClock,
  CalendarRange,
  Car,
  FileText,
  Fuel,
  LayoutDashboard,
  Map,
  Radar,
  Route,
  ScrollText,
  Settings,
  UserRound,
  Users,
  Wallet,
  Wrench,
  X,
} from "lucide-react";

import { useAuth } from "@/lib/auth";
import { canAccess } from "@/lib/rbac";
import { cn } from "@/lib/utils";

type Item = { href: string; label: string; icon: React.ElementType };
type Group = { title: string; items: Item[] };

const GROUPS: Group[] = [
  {
    title: "Pilotage",
    items: [
      { href: "/dashboard", label: "Tableau de bord", icon: LayoutDashboard },
      { href: "/fleet-control", label: "Centre de contrôle", icon: Radar },
      { href: "/map", label: "Carte temps réel", icon: Map },
    ],
  },
  {
    title: "Exploitation",
    items: [
      { href: "/reservations", label: "Réservations", icon: CalendarCheck },
      { href: "/planning-vehicles", label: "Planning véhicules", icon: CalendarRange },
      { href: "/planning-drivers", label: "Planning chauffeurs", icon: CalendarClock },
      { href: "/trips", label: "Courses", icon: Route },
    ],
  },
  {
    title: "Flotte",
    items: [
      { href: "/vehicles", label: "Véhicules", icon: Car },
      { href: "/drivers", label: "Chauffeurs", icon: UserRound },
      { href: "/maintenance", label: "Maintenance", icon: Wrench },
    ],
  },
  {
    title: "Finance",
    items: [
      { href: "/fuel", label: "Carburant", icon: Fuel },
      { href: "/expenses", label: "Dépenses", icon: Wallet },
      { href: "/reports", label: "Rapports", icon: FileText },
    ],
  },
  {
    title: "Organisation",
    items: [
      { href: "/subsidiaries", label: "Filiales", icon: Building2 },
      { href: "/employees", label: "Employés", icon: Users },
      { href: "/incidents", label: "Incidents", icon: AlertTriangle },
      { href: "/alerts", label: "Alertes", icon: Bell },
    ],
  },
  {
    title: "Système",
    items: [
      { href: "/notifications", label: "Notifications", icon: BellRing },
      { href: "/kbot", label: "K-BOT", icon: Bot },
      { href: "/audit", label: "Journal d'audit", icon: ScrollText },
      { href: "/settings", label: "Paramètres", icon: Settings },
    ],
  },
];

export function Sidebar({ open, onClose }: { open: boolean; onClose: () => void }) {
  const pathname = usePathname();
  const { me } = useAuth();

  // Navigation organisée par rôle : seuls les modules du métier apparaissent.
  const groups = GROUPS.map((g) => ({
    ...g,
    items: g.items.filter((i) => !me || canAccess(me.role, i.href)),
  })).filter((g) => g.items.length > 0);

  return (
    <>
      {open && (
        <div className="fixed inset-0 z-30 bg-navy-950/60 backdrop-blur-sm lg:hidden" onClick={onClose} aria-hidden />
      )}

      <aside
        className={cn(
          "fixed inset-y-0 left-0 z-40 flex w-64 flex-col bg-navy-900 text-slate-300 transition-transform lg:translate-x-0",
          open ? "translate-x-0" : "-translate-x-full",
        )}
      >
        <div className="flex h-16 shrink-0 items-center gap-2.5 border-b border-white/5 px-5">
          <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-white shadow-lg shadow-brand-600/20">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src="/logo-emblem.png" alt="Kaydan Express" className="h-8 w-8 object-contain" />
          </span>
          <div className="leading-tight">
            <p className="text-sm font-semibold text-white">Kaydan Express</p>
            <p className="text-[11px] text-slate-400">Fleet Management</p>
          </div>
          <button
            onClick={onClose}
            className="ml-auto rounded-md p-1.5 text-slate-400 hover:bg-white/10 lg:hidden"
            aria-label="Fermer le menu"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        <nav className="flex-1 space-y-5 overflow-y-auto px-3 py-4">
          {groups.map((group) => (
            <div key={group.title}>
              <p className="px-3 pb-1.5 text-[10px] font-semibold uppercase tracking-wider text-slate-500">
                {group.title}
              </p>
              <div className="space-y-0.5">
                {group.items.map(({ href, label, icon: Icon }) => {
                  const active = pathname === href || pathname.startsWith(href + "/");
                  return (
                    <Link
                      key={href}
                      href={href}
                      onClick={onClose}
                      className={cn(
                        "group relative flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors",
                        active
                          ? "bg-brand-500/15 text-white"
                          : "text-slate-300 hover:bg-white/5 hover:text-white",
                      )}
                    >
                      {active && (
                        <span className="absolute left-0 top-1/2 h-5 w-1 -translate-y-1/2 rounded-r-full bg-brand-500" />
                      )}
                      <Icon
                        className={cn(
                          "h-[18px] w-[18px] transition-colors",
                          active ? "text-brand-400" : "text-slate-400 group-hover:text-brand-300",
                        )}
                      />
                      {label}
                    </Link>
                  );
                })}
              </div>
            </div>
          ))}
        </nav>

        <div className="shrink-0 border-t border-white/5 px-5 py-3">
          {me && (
            <p className="mb-1 inline-flex items-center rounded-full bg-brand-500/15 px-2.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-brand-300">
              {me.role_display}
            </p>
          )}
          <p className="text-[11px] text-slate-500">Version 0.4 — UI Premium</p>
        </div>
      </aside>
    </>
  );
}
