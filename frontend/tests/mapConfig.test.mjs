// Tests de la configuration cartographique centralisée (runner natif de Node ≥ 22.18 :
// le module TypeScript est importé tel quel, types effacés). `npm run test:map`.
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  BASE_LAYER_KEYS, buildBaseLayers, cartoSource, checkCartoKey, keyFingerprint, satelliteSource, shouldFallback,
  usableSources,
} from "../src/lib/mapConfig.ts";

const KEY = "kx-test/key+42";

test("sans clé CARTO : aucune requête anonyme (pas de tuile filigranée), secours directs", () => {
  for (const key of [undefined, "", "   "]) {
    const layers = buildBaseLayers({ cartoApiKey: key });
    assert.equal(layers.plan.sources[0].id, "osm-standard");
    assert.equal(layers.dark.sources[0].id, "esri-dark-gray");
    for (const layer of Object.values(layers)) {
      for (const source of layer.sources) assert.ok(!source.url.includes("cartocdn"), source.id);
    }
  }
});

test("avec clé : endpoints CARTO officiels Voyager / Dark Matter, clé sur chaque tuile", () => {
  const layers = buildBaseLayers({ cartoApiKey: KEY });
  const plan = layers.plan.sources[0];
  const dark = layers.dark.sources[0];
  assert.equal(plan.url,
    "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png?key=kx-test%2Fkey%2B42");
  assert.equal(dark.url, "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png?key=kx-test%2Fkey%2B42");
  for (const source of [plan, dark]) {
    assert.equal(source.subdomains, "abcd");
    assert.match(source.attribution, /OpenStreetMap/);
    assert.match(source.attribution, /CARTO/);
    assert.equal(source.maxNativeZoom, 20);
  }
  assert.deepEqual(layers.plan.sources.map((s) => s.id), ["carto-voyager", "osm-standard"]);
  assert.deepEqual(layers.dark.sources.map((s) => s.id), ["carto-dark_matter", "esri-dark-gray", "osm-standard"]);
  assert.equal(cartoSource("voyager", "  "), null);
});

test("Referer d'origine envoyé à chaque fournisseur (restriction de domaine CARTO)", () => {
  for (const layer of Object.values(buildBaseLayers({ cartoApiKey: KEY }))) {
    for (const source of layer.sources) assert.equal(source.referrerPolicy, "strict-origin-when-cross-origin");
  }
});

test("satellite : fournisseur distinct, Esri par défaut, surchargeable, secours vers le Plan", () => {
  const byDefault = buildBaseLayers({ cartoApiKey: KEY }).satellite;
  assert.equal(byDefault.sources[0].id, "esri-imagery");
  assert.match(byDefault.sources[0].url, /World_Imagery/);
  assert.deepEqual(byDefault.sources.slice(1).map((s) => s.id), ["carto-voyager", "osm-standard"]);
  const custom = satelliteSource({ satelliteUrl: "https://tiles.example/{z}/{x}/{y}.jpg",
                                   satelliteAttribution: "© Exemple", satelliteMaxZoom: "18" });
  assert.equal(custom.url, "https://tiles.example/{z}/{x}/{y}.jpg");
  assert.equal(custom.attribution, "© Exemple");
  assert.equal(custom.maxNativeZoom, 18);
  assert.equal(satelliteSource({ satelliteMaxZoom: "99" }).maxNativeZoom, 19);
});

test("trois fonds, dans l'ordre du sélecteur", () => {
  assert.deepEqual(BASE_LAYER_KEYS, ["plan", "dark", "satellite"]);
  const layers = buildBaseLayers({});
  assert.deepEqual(BASE_LAYER_KEYS.map((k) => layers[k].label), ["Plan", "Sombre", "Satellite"]);
});

test("repli : seulement sur des échecs en série, jamais sur une erreur isolée", () => {
  assert.equal(shouldFallback(1, 0), false);
  assert.equal(shouldFallback(2, 0), false);
  assert.equal(shouldFallback(3, 0), true);
  assert.equal(shouldFallback(3, 1), false);
  assert.equal(shouldFallback(7, 2), false);
  assert.equal(shouldFallback(8, 7), true);
  assert.equal(shouldFallback(8, 40), false);
});

test("aucune clé de fond de carte codée en dur dans le frontend", () => {
  const offenders = [];
  const walk = (dir) => {
    for (const name of readdirSync(dir)) {
      const path = join(dir, name);
      if (statSync(path).isDirectory()) walk(path);
      else if (/\.(ts|tsx|js|mjs)$/.test(name) && /[?&](?:key|api_key|access_token)=[A-Za-z0-9_-]{8,}/.test(readFileSync(path, "utf8"))) {
        offenders.push(path);
      }
    }
  };
  walk(fileURLToPath(new URL("../src", import.meta.url)));
  assert.deepEqual(offenders, []);
});

// --- Vérification de la clé (tuiles filigranées servies en HTTP 200) ------------------

function fakeFetch({ keyedBody = "carte", anonymousBody = "filigrane", keyedStatus = 200, fail = false } = {}) {
  const calls = [];
  const impl = async (url, init) => {
    calls.push({ url, init });
    if (fail) throw new TypeError("réseau");
    const keyed = new URL(url).searchParams.has("key");
    const body = new TextEncoder().encode(keyed ? keyedBody : anonymousBody);
    const status = keyed ? keyedStatus : 200;
    return new Response(status === 200 ? body : null, { status });
  };
  return { impl, calls };
}

test("clé reconnue : tuile différente de la tuile anonyme → valid", async () => {
  const { impl, calls } = fakeFetch();
  assert.equal(await checkCartoKey(KEY, impl), "valid");
  assert.equal(calls.length, 2);
  assert.ok(calls.every((c) => c.init.mode === "cors" && c.init.referrerPolicy === "strict-origin-when-cross-origin"));
  assert.ok(calls.every((c) => c.init.cache === "no-store"), "aucun cache ne fausse le verdict");
  assert.ok(calls.some((c) => new URL(c.url).searchParams.get("key") === KEY));
});

test("clé inconnue : même tuile filigranée qu'en anonyme → rejected", async () => {
  assert.equal(await checkCartoKey(KEY, fakeFetch({ keyedBody: "filigrane" }).impl), "rejected");
});

test("clé restreinte à d'autres domaines (403) → rejected ; réseau, 5xx → unknown ; sans clé → rejected", async () => {
  assert.equal(await checkCartoKey(KEY, fakeFetch({ keyedStatus: 403 }).impl), "rejected");
  assert.equal(await checkCartoKey(KEY, fakeFetch({ keyedStatus: 503 }).impl), "unknown");
  assert.equal(await checkCartoKey(KEY, fakeFetch({ fail: true }).impl), "unknown");
  const { impl, calls } = fakeFetch();
  assert.equal(await checkCartoKey("  ", impl), "rejected");
  assert.equal(calls.length, 0);
});

test("fonds utilisables selon le statut : refusée → sans CARTO ; en cours → on attend CARTO", () => {
  const layers = buildBaseLayers({ cartoApiKey: KEY });
  assert.deepEqual(usableSources(layers.plan, "rejected").map((s) => s.id), ["osm-standard"]);
  assert.deepEqual(usableSources(layers.dark, "rejected").map((s) => s.id), ["esri-dark-gray", "osm-standard"]);
  assert.deepEqual(usableSources(layers.satellite, "rejected").map((s) => s.id), ["esri-imagery", "osm-standard"]);
  assert.deepEqual(usableSources(layers.plan, "pending"), []);
  assert.equal(usableSources(layers.satellite, "pending")[0].id, "esri-imagery");
  assert.equal(usableSources(layers.plan, "valid")[0].id, "carto-voyager");
  assert.equal(usableSources(layers.plan, "unknown")[0].id, "carto-voyager");
});

test("empreinte de cache : stable, distincte par clé, sans la clé", () => {
  assert.equal(keyFingerprint(KEY), keyFingerprint(` ${KEY} `));
  assert.notEqual(keyFingerprint(KEY), keyFingerprint(`${KEY}x`));
  assert.match(keyFingerprint(KEY), /^[0-9a-f]{8}$/);
  assert.ok(!keyFingerprint(KEY).includes("kx"));
});

// --- Conditions CARTO : pas de fond CARTO en véhicule (§ 14) ---------------------------

test("vue en véhicule : jamais CARTO, même avec une clé ; fournisseur de navigation configurable", () => {
  const vehicle = buildBaseLayers({ cartoApiKey: KEY }, "vehicle");
  for (const layer of Object.values(vehicle)) {
    for (const source of layer.sources) assert.ok(!source.url.includes("cartocdn"), `${layer.key}:${source.id}`);
  }
  assert.deepEqual(vehicle.plan.sources.map((s) => s.id), ["osm-standard"]);
  assert.deepEqual(vehicle.dark.sources.map((s) => s.id), ["esri-dark-gray", "osm-standard"]);
  assert.deepEqual(vehicle.satellite.sources.map((s) => s.id), ["esri-imagery", "osm-standard"]);
  const custom = buildBaseLayers({ cartoApiKey: KEY, vehicleUrl: "https://nav.example/{z}/{x}/{y}.png",
                                   vehicleAttribution: "© Nav" }, "vehicle");
  assert.deepEqual(custom.plan.sources.map((s) => s.id), ["vehicle-custom", "osm-standard"]);
  assert.equal(custom.plan.sources[0].attribution, "© Nav");
  // Au bureau, CARTO reste le fond préféré.
  assert.equal(buildBaseLayers({ cartoApiKey: KEY }, "office").plan.sources[0].id, "carto-voyager");
});

test("le service worker ne met jamais en cache les tuiles d'un autre domaine (CARTO § 9)", () => {
  const sw = readFileSync(fileURLToPath(new URL("../public/sw.js", import.meta.url)), "utf8");
  assert.match(sw, /origin === self\.location\.origin/);
  assert.match(sw, /sameOrigin && \(/);
  assert.match(sw, /response\.ok/);
});
