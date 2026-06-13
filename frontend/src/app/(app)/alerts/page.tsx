"use client";

import {
  AlertTriangle,
  Clock,
  FileWarning,
  IdCard,
  Wrench,
} from "lucide-react";

import { Card, CardBody, EmptyState, Spinner } from "@/components/ui";
import { useAlerts } from "@/lib/queries";
import { cn, formatDate } from "@/lib/utils";

const TYPE_ICON: Record<string, React.ElementType> = {
  document: FileWarning,
  license: IdCard,
  maintenance: Wrench,
  late: Clock,
};

export default function AlertsPage() {
  const { data, isLoading } = useAlerts();

  if (isLoading) {
    return (
      <div className="flex justify-center py-20">
        <Spinner className="h-8 w-8" />
      </div>
    );
  }

  const alerts = data?.results ?? [];
  const counts = data?.counts ?? { critical: 0, warning: 0, total: 0 };

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-3 gap-3">
        <Card>
          <CardBody className="py-3 text-center">
            <p className="text-2xl font-bold text-rose-600">{counts.critical}</p>
            <p className="text-xs text-muted">Critiques</p>
          </CardBody>
        </Card>
        <Card>
          <CardBody className="py-3 text-center">
            <p className="text-2xl font-bold text-amber-600">{counts.warning}</p>
            <p className="text-xs text-muted">Avertissements</p>
          </CardBody>
        </Card>
        <Card>
          <CardBody className="py-3 text-center">
            <p className="text-2xl font-bold text-ink">{counts.total}</p>
            <p className="text-xs text-muted">Total</p>
          </CardBody>
        </Card>
      </div>

      <Card>
        <CardBody className="p-0">
          {alerts.length === 0 ? (
            <EmptyState title="Aucune alerte" hint="Tout est sous contrôle 🎉" />
          ) : (
            <ul className="divide-y divide-line">
              {alerts.map((a, i) => {
                const Icon = TYPE_ICON[a.type] ?? AlertTriangle;
                const critical = a.severity === "critical";
                return (
                  <li key={i} className="flex items-center gap-3 px-5 py-3.5 hover:bg-surface2">
                    <span
                      className={cn(
                        "flex h-9 w-9 shrink-0 items-center justify-center rounded-lg",
                        critical ? "bg-rose-500/10 text-rose-600" : "bg-amber-500/10 text-amber-600",
                      )}
                    >
                      <Icon className="h-4 w-4" />
                    </span>
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm font-medium text-ink">{a.title}</p>
                      <p className="text-xs text-muted">{a.detail}</p>
                    </div>
                    <div className="text-right">
                      <span
                        className={cn(
                          "rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset",
                          critical
                            ? "bg-rose-500/10 text-rose-600 ring-rose-500/20"
                            : "bg-amber-500/10 text-amber-600 ring-amber-500/20",
                        )}
                      >
                        {critical ? "Critique" : "À surveiller"}
                      </span>
                      <p className="mt-1 text-[11px] text-faint">{formatDate(a.date)}</p>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </CardBody>
      </Card>
    </div>
  );
}
