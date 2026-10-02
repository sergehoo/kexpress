#!/usr/bin/env node
/**
 * Vérifie la clé CARTO des fonds de carte — sans jamais l'afficher, et sans en-tête Referer
 * forgé (interdit par les conditions CARTO, § 9).
 *
 *   npm run check:carto                        # clé lue dans l'environnement ou .env.local
 *   npm run check:carto -- --allow-unrestricted
 *
 * Clé : NEXT_PUBLIC_CARTO_API_KEY (environnement, puis frontend/.env.local, puis frontend/.env).
 * Pour chaque fond (Voyager, Dark Matter), une tuile est demandée SANS Referer :
 * - HTTP 403 → la clé est restreinte à des domaines : c'est la configuration attendue. Sa
 *   validité pour CHAQUE domaine se constate dans le navigateur (l'application vérifie la clé
 *   à l'ouverture d'une carte et l'indique en console) ou dans dashboard.basemaps.carto.com ;
 * - HTTP 200 et tuile IDENTIQUE à la tuile anonyme → clé inconnue ou inactive (CARTO renvoie
 *   la même tuile filigranée « API KEY REQUIRED » sans clé et avec une clé inconnue) ;
 * - HTTP 200 et tuile différente → clé valide mais NON restreinte : à restreindre aux
 *   domaines de l'application (échec, sauf --allow-unrestricted).
 * Code de sortie : 0 si chaque fond est conforme, 1 sinon (y compris réponse non concluante).
 */
import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const TILE = "12/1998/1978"; // Abidjan, zoom 12
const STYLES = { "CARTO Voyager": "rastertiles/voyager", "CARTO Dark Matter": "dark_all" };
const allowUnrestricted = process.argv.includes("--allow-unrestricted");

function readKey() {
  if (process.env.NEXT_PUBLIC_CARTO_API_KEY?.trim()) return process.env.NEXT_PUBLIC_CARTO_API_KEY.trim();
  for (const name of [".env.local", ".env"]) {
    const path = fileURLToPath(new URL(`../${name}`, import.meta.url));
    if (!existsSync(path)) continue;
    const line = readFileSync(path, "utf8").split(/\r?\n/).find((l) => /^\s*NEXT_PUBLIC_CARTO_API_KEY\s*=/.test(l));
    const value = line?.split("=").slice(1).join("=").trim().replace(/^["']|["']$/g, "");
    if (value) return value;
  }
  return "";
}

async function fetchTile(style, key) {
  const url = `https://a.basemaps.cartocdn.com/${style}/${TILE}.png${key ? `?key=${encodeURIComponent(key)}` : ""}`;
  try {
    const res = await fetch(url, { headers: { "User-Agent": "kexpress-check-carto" }, cache: "no-store" });
    const body = Buffer.from(await res.arrayBuffer());
    return { status: res.status, hash: createHash("sha256").update(body).digest("hex") };
  } catch (err) {
    return { status: 0, hash: "", error: err?.cause?.code || err?.name || "réseau" };
  }
}

const key = readKey();
if (!key) {
  console.error("NEXT_PUBLIC_CARTO_API_KEY absente : les fonds Plan et Sombre utiliseront les fournisseurs de secours.");
  process.exit(1);
}
console.log(`Clé CARTO présente (${key.length} caractères, non affichée).`);

let ok = true;
for (const [label, style] of Object.entries(STYLES)) {
  const [anonymous, keyed] = await Promise.all([fetchTile(style, ""), fetchTile(style, key)]);
  let verdict;
  if (keyed.status === 403) {
    verdict = "OK — clé restreinte par domaine (vérifiez ses domaines autorisés dans le tableau de bord CARTO)";
  } else if (anonymous.status !== 200 || keyed.status !== 200) {
    verdict = `NON CONCLUANT — HTTP ${keyed.status || anonymous.status}${keyed.error ? ` (${keyed.error})` : ""}`;
    ok = false;
  } else if (keyed.hash === anonymous.hash) {
    verdict = "ÉCHEC — tuile filigranée « API KEY REQUIRED » : clé inconnue ou inactive";
    ok = false;
  } else if (allowUnrestricted) {
    verdict = "OK — clé valide, NON restreinte (restriction de domaine conseillée)";
  } else {
    verdict = "ÉCHEC — clé valide mais NON restreinte : limitez-la aux domaines de l'application";
    ok = false;
  }
  console.log(`${label} : ${verdict}`);
}
process.exit(ok ? 0 : 1);
