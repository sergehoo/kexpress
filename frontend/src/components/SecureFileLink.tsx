"use client";

import { useState } from "react";

import { openSecureFile } from "@/lib/api";

/** Lien « Voir » vers un fichier protégé : ouverture authentifiée, jamais d'URL publique. */
export function SecureFileLink({ url, label = "Voir" }: { url: string; label?: string }) {
  const [error, setError] = useState<string | null>(null);
  return (
    <span className="inline-flex flex-col items-end">
      <button
        type="button"
        onClick={() => { setError(null); openSecureFile(url).catch((e: Error) => setError(e.message)); }}
        className="text-xs font-medium text-brand-600 hover:underline"
      >
        {label}
      </button>
      {error && <span className="text-[10px] text-red-600">{error}</span>}
    </span>
  );
}
