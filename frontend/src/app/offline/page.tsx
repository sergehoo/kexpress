import { WifiOff } from "lucide-react";

export const metadata = { title: "Hors ligne — Kaydan Express" };

export default function OfflinePage() {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-3 p-6 text-center">
      <span className="flex h-14 w-14 items-center justify-center rounded-2xl bg-amber-50 text-amber-600">
        <WifiOff className="h-7 w-7" />
      </span>
      <h1 className="text-lg font-semibold text-ink">Vous êtes hors ligne</h1>
      <p className="max-w-sm text-sm text-muted">
        Les dernières données consultées restent disponibles. La synchronisation reprendra
        automatiquement dès le retour de la connexion.
      </p>
    </div>
  );
}
