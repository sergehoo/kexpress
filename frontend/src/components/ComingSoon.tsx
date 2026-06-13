import { Card, CardBody } from "@/components/ui";
import type { LucideIcon } from "lucide-react";

export function ComingSoon({
  title,
  description,
  icon: Icon,
  features,
}: {
  title: string;
  description: string;
  icon: LucideIcon;
  features?: string[];
}) {
  return (
    <Card className="animate-fade-up">
      <CardBody className="flex flex-col items-center gap-4 py-14 text-center">
        <span className="flex h-16 w-16 items-center justify-center rounded-2xl bg-gradient-to-br from-brand-500/15 to-brand-600/10 text-brand-600">
          <Icon className="h-8 w-8" />
        </span>
        <div>
          <h2 className="text-lg font-semibold text-ink">{title}</h2>
          <p className="mx-auto mt-1 max-w-md text-sm text-muted">{description}</p>
        </div>
        <span className="rounded-full bg-brand-500/10 px-3 py-1 text-xs font-medium text-brand-600">
          Module en cours de déploiement
        </span>
        {features && features.length > 0 && (
          <ul className="mt-2 grid max-w-md gap-2 text-left sm:grid-cols-2">
            {features.map((f) => (
              <li key={f} className="flex items-center gap-2 text-sm text-muted">
                <span className="h-1.5 w-1.5 rounded-full bg-brand-500" />
                {f}
              </li>
            ))}
          </ul>
        )}
      </CardBody>
    </Card>
  );
}
