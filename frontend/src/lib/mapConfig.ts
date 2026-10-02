/**
 * Configuration cartographique CENTRALISÉE — fonds de carte de toutes les cartes
 * (Centre de contrôle, Carte temps réel, fiche course, relecture d'itinéraire).
 *
 * - Plan : CARTO Voyager ; Sombre : CARTO Dark Matter — endpoints officiels
 *   `{s}.basemaps.cartocdn.com`, clé passée en paramètre `key` sur chaque tuile.
 * - Satellite : fournisseur distinct, configurable (Esri World Imagery par défaut).
 * - Secours : si un fournisseur échoue (ou si la clé CARTO manque), la carte bascule sur le
 *   fournisseur suivant de la chaîne — jamais de tuile filigranée « API KEY REQUIRED ».
 *
 * La clé CARTO est une clé de NAVIGATEUR (publique par conception : elle figure dans l'URL de
 * chaque tuile). Elle se protège dans le tableau de bord CARTO en la restreignant aux domaines
 * de l'application (en-tête Referer) — d'où `referrerPolicy` ci-dessous, qui garantit l'envoi de
 * l'origine même si la page impose une politique plus stricte. Elle ne doit être ni codée en dur
 * ni journalisée : ce module ne l'écrit jamais dans la console.
 *
 * Conditions CARTO (carto.com/legal/basemap-terms) : pas de fond CARTO pour la navigation
 * en temps réel d'un véhicule ni sur un véhicule en mouvement (§ 14) → contexte « vehicle »
 * (chauffeur, suivi de course avec guidage) servi SANS CARTO ; pas de cache de tuiles de plus
 * de 30 jours sur l'appareil (§ 9 : le service worker ne met plus en cache les tuiles tierces) ;
 * attribution OpenStreetMap + CARTO visible (§ 13) ; une clé propre à chaque environnement (§ 8).
 *
 * Module SANS dépendance (ni React ni Leaflet) : testable seul (`npm run test:map`). Les
 * itinéraires et distances restent calculés par OSRM côté serveur : rien ici ne les concerne.
 */

export type BaseLayerKey = "plan" | "dark" | "satellite";
export type TileReferrerPolicy = Exclude<ReferrerPolicy, "">;

export interface TileSource {
  /** Identifiant stable (clé React du calque, diagnostic) — jamais l'URL, qui porte la clé. */
  id: string;
  /** Nom lisible du fournisseur (affiché lors d'un repli). */
  provider: string;
  url: string;
  attribution: string;
  subdomains?: string;
  maxNativeZoom: number;
  /** Politique Referer des tuiles : l'origine part vers le fournisseur (restriction de domaine). */
  referrerPolicy: TileReferrerPolicy;
}

export interface BaseLayer {
  key: BaseLayerKey;
  label: string;
  /** Fournisseurs dans l'ordre de préférence ; le premier qui répond est affiché. */
  sources: TileSource[];
}

export interface MapEnv {
  cartoApiKey?: string;
  satelliteUrl?: string;
  satelliteAttribution?: string;
  satelliteMaxZoom?: string;
  /** Fond « Plan » des vues EN VÉHICULE (fournisseur autorisant la navigation) ; vide = OSM. */
  vehicleUrl?: string;
  vehicleAttribution?: string;
}

/** `office` : cartes de suivi au bureau ; `vehicle` : chauffeur / guidage en course (sans CARTO). */
export type MapContext = "office" | "vehicle";

/** Zoom maximal de la carte : au-delà du zoom natif d'un fournisseur, ses tuiles sont agrandies. */
export const MAP_MAX_ZOOM = 20;
/** Centre par défaut (Abidjan) quand aucune position n'est connue. */
export const DEFAULT_CENTER: [number, number] = [5.345, -4.024];
export const DEFAULT_ZOOM = 12;

const REFERRER: TileReferrerPolicy = "strict-origin-when-cross-origin";
const CARTO_ATTRIBUTION =
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, ' +
  '&copy; <a href="https://carto.com/attributions">CARTO</a>';
const OSM_ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';
const ESRI_IMAGERY_URL =
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";
const ESRI_IMAGERY_ATTRIBUTION =
  "Powered by Esri &mdash; Source : Esri, Vantor, Earthstar Geographics, and the GIS User Community";

/** Variables d'environnement lues à la COMPILATION (Next.js remplace les `process.env.NEXT_PUBLIC_*`
 *  écrits littéralement). Valeur vide ou absente = non configuré. */
export const MAP_ENV: MapEnv = {
  cartoApiKey: process.env.NEXT_PUBLIC_CARTO_API_KEY,
  satelliteUrl: process.env.NEXT_PUBLIC_MAP_SATELLITE_URL,
  satelliteAttribution: process.env.NEXT_PUBLIC_MAP_SATELLITE_ATTRIBUTION,
  satelliteMaxZoom: process.env.NEXT_PUBLIC_MAP_SATELLITE_MAX_ZOOM,
  vehicleUrl: process.env.NEXT_PUBLIC_MAP_VEHICLE_URL,
  vehicleAttribution: process.env.NEXT_PUBLIC_MAP_VEHICLE_ATTRIBUTION,
};

function clean(value: string | undefined): string {
  return (value ?? "").trim();
}

/** Tuile CARTO officielle (raster), clé en paramètre `key` — `null` sans clé : pas de requête
 *  anonyme, qui reviendrait filigranée. */
export function cartoSource(style: "voyager" | "dark_matter", apiKey: string | undefined): TileSource | null {
  const key = clean(apiKey);
  if (!key) return null;
  const path = style === "voyager" ? "rastertiles/voyager" : "dark_all";
  return {
    id: `carto-${style}`,
    provider: style === "voyager" ? "CARTO Voyager" : "CARTO Dark Matter",
    url: `https://{s}.basemaps.cartocdn.com/${path}/{z}/{x}/{y}{r}.png?key=${encodeURIComponent(key)}`,
    attribution: CARTO_ATTRIBUTION,
    subdomains: "abcd",
    maxNativeZoom: 20,
    referrerPolicy: REFERRER,
  };
}

const OSM_STANDARD: TileSource = {
  id: "osm-standard",
  provider: "OpenStreetMap",
  url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
  attribution: OSM_ATTRIBUTION,
  maxNativeZoom: 19,
  referrerPolicy: REFERRER,
};

const ESRI_DARK_GRAY: TileSource = {
  id: "esri-dark-gray",
  provider: "Esri Dark Gray",
  url: "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}",
  attribution: "Powered by Esri &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors, and the GIS user community",
  maxNativeZoom: 16,
  referrerPolicy: REFERRER,
};

/** Satellite : fournisseur configuré à part (URL Leaflet `{z}/{x}/{y}` ou `{z}/{y}/{x}`). */
export function satelliteSource(env: MapEnv): TileSource {
  const url = clean(env.satelliteUrl);
  const zoom = Number.parseInt(clean(env.satelliteMaxZoom), 10);
  return {
    id: url ? "satellite-custom" : "esri-imagery",
    provider: url ? "Satellite" : "Esri World Imagery",
    url: url || ESRI_IMAGERY_URL,
    attribution: clean(env.satelliteAttribution) || (url ? "Imagerie satellite" : ESRI_IMAGERY_ATTRIBUTION),
    maxNativeZoom: Number.isFinite(zoom) && zoom > 0 && zoom <= MAP_MAX_ZOOM ? zoom : 19,
    referrerPolicy: REFERRER,
  };
}

/** Fond « Plan » des vues en véhicule : fournisseur configuré, sinon OpenStreetMap. */
function vehicleSource(env: MapEnv): TileSource | null {
  const url = clean(env.vehicleUrl);
  if (!url) return null;
  return { id: "vehicle-custom", provider: "Plan (navigation)", url,
           attribution: clean(env.vehicleAttribution) || OSM_ATTRIBUTION, maxNativeZoom: 19, referrerPolicy: REFERRER };
}

/** Les trois fonds, chacun avec sa chaîne de secours. Contexte `vehicle` : jamais CARTO. */
export function buildBaseLayers(env: MapEnv = MAP_ENV, context: MapContext = "office"): Record<BaseLayerKey, BaseLayer> {
  const vehicle = context === "vehicle";
  const voyager = vehicle ? vehicleSource(env) : cartoSource("voyager", env.cartoApiKey);
  const darkMatter = vehicle ? null : cartoSource("dark_matter", env.cartoApiKey);
  const planChain = [voyager, OSM_STANDARD].filter(Boolean) as TileSource[];
  return {
    plan: { key: "plan", label: "Plan", sources: planChain },
    dark: { key: "dark", label: "Sombre", sources: [darkMatter, ESRI_DARK_GRAY, OSM_STANDARD].filter(Boolean) as TileSource[] },
    satellite: { key: "satellite", label: "Satellite", sources: [satelliteSource(env), ...planChain] },
  };
}

export const BASE_LAYERS = buildBaseLayers();
export const VEHICLE_BASE_LAYERS = buildBaseLayers(MAP_ENV, "vehicle");
export const BASE_LAYER_KEYS: BaseLayerKey[] = ["plan", "dark", "satellite"];

/** La clé CARTO est-elle configurée ? (sans elle, Plan et Sombre partent directement en secours). */
export const CARTO_CONFIGURED = Boolean(clean(MAP_ENV.cartoApiKey));

/**
 * Décision de repli : un fournisseur est déclaré indisponible quand ses tuiles échouent en
 * série — 3 échecs sans aucune tuile chargée (fournisseur injoignable, clé refusée en 403), ou
 * 8 échecs dépassant les tuiles chargées (panne en cours de session). Une erreur isolée (tuile
 * hors couverture, réseau mobile instable) ne fait pas basculer la carte.
 */
export function shouldFallback(errors: number, loads: number): boolean {
  return (errors >= 3 && loads === 0) || (errors >= 8 && errors > loads);
}

// --- Vérification de la clé CARTO (une fois par session) -------------------------------

/** `pending` : vérification en cours (CARTO pas encore affiché, pour ne jamais montrer de
 *  tuile filigranée) ; `unknown` : vérification impossible (CARTO affiché, repli sur erreurs). */
export type CartoKeyStatus = "valid" | "rejected" | "unknown" | "pending";

/** Délai maximal de la vérification avant d'afficher CARTO quand même. */
export const CARTO_CHECK_TIMEOUT_MS = 1500;

/** Tuile de référence (Abidjan, zoom 12) pour la vérification de la clé. */
const PROBE_TILE = "https://a.basemaps.cartocdn.com/rastertiles/voyager/12/1998/1978.png";

async function sha256Hex(data: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

/**
 * CARTO répond 200 avec la MÊME tuile filigranée « API KEY REQUIRED » sans clé et avec une clé
 * inconnue : une erreur HTTP ne le révèle pas. On compare donc une tuile servie avec la clé à
 * la même tuile anonyme (CARTO autorise la lecture CORS) : identiques = clé refusée. Réseau
 * indisponible ou réponse inattendue = « unknown » (on garde CARTO ; le repli sur erreurs de
 * tuiles reste actif). 403 = clé restreinte à d'autres domaines.
 */
export async function checkCartoKey(
  apiKey: string | undefined,
  fetchImpl: typeof fetch = fetch,
): Promise<CartoKeyStatus> {
  const key = clean(apiKey);
  if (!key) return "rejected";
  try {
    // `no-store` : ni le cache HTTP (24 h chez CARTO) ni un ancien filigrane ne faussent le verdict.
    const init: RequestInit = { mode: "cors", referrerPolicy: REFERRER, cache: "no-store" };
    const [keyed, anonymous] = await Promise.all([
      fetchImpl(`${PROBE_TILE}?key=${encodeURIComponent(key)}`, init),
      fetchImpl(PROBE_TILE, init),
    ]);
    if (keyed.status === 401 || keyed.status === 403) return "rejected";
    if (!keyed.ok || !anonymous.ok) return "unknown";
    const [a, b] = await Promise.all([keyed.arrayBuffer(), anonymous.arrayBuffer()]);
    return (await sha256Hex(a)) === (await sha256Hex(b)) ? "rejected" : "valid";
  } catch {
    return "unknown";
  }
}

/** Empreinte courte et synchrone de la clé (FNV-1a 32 bits) pour le cache de session : elle
 *  distingue deux clés sans jamais stocker la clé elle-même. */
export function keyFingerprint(apiKey: string): string {
  let hash = 0x811c9dc5;
  for (const char of clean(apiKey)) {
    hash ^= char.codePointAt(0) ?? 0;
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(16).padStart(8, "0");
}

/** Fonds utilisables selon le statut de la clé : refusée → sans CARTO ; vérification en cours →
 *  aucun fond CARTO encore (liste vide si le fond commence par CARTO : on attend). */
export function usableSources(layer: BaseLayer, status: CartoKeyStatus): TileSource[] {
  if (status === "pending") return layer.sources[0]?.id.startsWith("carto-") ? [] : layer.sources;
  if (status !== "rejected") return layer.sources;
  const rest = layer.sources.filter((source) => !source.id.startsWith("carto-"));
  return rest.length ? rest : layer.sources;
}
