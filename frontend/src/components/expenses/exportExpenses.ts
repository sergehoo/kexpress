import { api, apiError } from "@/lib/api";

/** Téléchargement de l'export CSV des dépenses, filtres courants appliqués.
 *
 *  Passe par le client API authentifié (en-tête JWT) puis une URL objet temporaire : un
 *  simple lien vers `/api/expenses/export/` partirait sans la session et serait refusé. */
export async function downloadExpensesCsv(filters: Record<string, string>): Promise<void> {
  const params = { ...filters };
  delete params.page_size; // l'export porte sur tout le périmètre filtré, pas sur une page
  try {
    const res = await api.get<Blob>("/expenses/export/", { params, responseType: "blob" });
    const url = URL.createObjectURL(res.data);
    const a = document.createElement("a");
    a.href = url;
    a.download = `depenses_${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10_000);
  } catch (err) {
    // En `blob`, le corps d'erreur DRF arrive lui aussi en Blob : on le relit pour en
    // extraire le message plutôt que d'afficher une erreur générique.
    const data = (err as { response?: { data?: unknown } })?.response?.data;
    if (data instanceof Blob) {
      try {
        const parsed = JSON.parse(await data.text()) as { detail?: string };
        if (parsed.detail) throw new Error(parsed.detail);
      } catch (inner) {
        if (inner instanceof Error && !(inner instanceof SyntaxError)) throw inner;
      }
    }
    throw new Error(apiError(err, "Export impossible."));
  }
}
