// Recherche dans les référentiels (autocomplétion des formulaires #2/#3/#4).
import { api } from "@/lib/api";

type Named = { id: string; name: string; city?: string };
type Option = { value: string; label: string };

const toOptions = (rows: Named[]): Option[] => rows.map((r) => ({ value: r.name, label: r.name }));

/** Marques (#2). */
export async function searchBrands(q: string): Promise<Option[]> {
  const { data } = await api.get<Named[]>("/vehicle-brands/", { params: { search: q } });
  return toOptions(data);
}

/** Modèles filtrés par la marque sélectionnée (#2, cascade). */
export async function searchModels(q: string, brandName: string): Promise<Option[]> {
  const name = brandName.trim();
  if (!name) return [];
  const { data: brands } = await api.get<Named[]>("/vehicle-brands/", { params: { search: name } });
  const brand =
    brands.find((b) => b.name.toLowerCase() === name.toLowerCase()) ?? brands[0];
  if (!brand) return []; // marque libre absente du référentiel : pas de suggestion de modèle
  const { data } = await api.get<Named[]>("/vehicle-models/", {
    params: { brand: brand.id, search: q },
  });
  return toOptions(data);
}

/** Compagnies d'assurance (#3). */
export async function searchInsuranceCompanies(q: string): Promise<Option[]> {
  const { data } = await api.get<Named[]>("/insurance-companies/", { params: { search: q } });
  return toOptions(data);
}

/** Centres de visite technique (#4). */
export async function searchInspectionCenters(q: string): Promise<Option[]> {
  const { data } = await api.get<Named[]>("/inspection-centers/", { params: { search: q } });
  return data.map((c) => ({ value: c.name, label: c.city ? `${c.name} — ${c.city}` : c.name }));
}
