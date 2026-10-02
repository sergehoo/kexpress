"use client";

import { useEffect } from "react";

import { oidcCompleteSilent } from "@/lib/oidc";

/** Retour du renouvellement silencieux Keycloak (iframe `prompt=none`) : transmet le
 *  résultat à la fenêtre parente. Aucun affichage, aucune initialisation de session ici. */
export default function SilentCallbackPage() {
  useEffect(() => {
    void oidcCompleteSilent().catch(() => undefined);
  }, []);
  return null;
}
