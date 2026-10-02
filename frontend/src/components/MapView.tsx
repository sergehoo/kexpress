"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Circle, MapContainer, Marker, Polygon, Polyline, TileLayer, Tooltip, useMap, useMapEvents } from "react-leaflet";
import L from "leaflet";
import "leaflet/dist/leaflet.css";

import {
  BASE_LAYER_KEYS, BASE_LAYERS, CARTO_CHECK_TIMEOUT_MS, CARTO_CONFIGURED, DEFAULT_CENTER, DEFAULT_ZOOM, MAP_ENV,
  MAP_MAX_ZOOM, VEHICLE_BASE_LAYERS,
  checkCartoKey, keyFingerprint, shouldFallback, usableSources,
  type BaseLayer, type BaseLayerKey, type CartoKeyStatus,
} from "@/lib/mapConfig";
import type { VehiclePosition } from "@/lib/types";

/** Statut de la clé CARTO, vérifié UNE fois par session (toutes cartes confondues) : une clé
 *  refusée renvoie des tuiles filigranées en HTTP 200 — seul ce contrôle permet de les éviter.
 *  Passé le délai d'attente, la carte s'affiche (statut « unknown ») ; le verdict arrivé ensuite
 *  est quand même retenu et diffusé aux cartes ouvertes. */
let cartoCheck: Promise<CartoKeyStatus> | null = null;
const cartoListeners = new Set<(status: CartoKeyStatus) => void>();
const CARTO_CHECK_STORAGE = "kx-carto-key";
let cartoMissingWarned = false;

function cachedCartoStatus(): CartoKeyStatus | null {
  try {
    const fingerprint = keyFingerprint(MAP_ENV.cartoApiKey ?? "");
    const cached = sessionStorage.getItem(CARTO_CHECK_STORAGE);
    if (cached?.startsWith(`${fingerprint}:`)) return cached.slice(fingerprint.length + 1) as CartoKeyStatus;
  } catch {
    /* stockage indisponible (navigation privée) : on vérifie à chaque chargement */
  }
  return null;
}

function startCartoCheck(): Promise<CartoKeyStatus> {
  const key = MAP_ENV.cartoApiKey ?? "";
  const fingerprint = keyFingerprint(key);
  const verdict = checkCartoKey(key).then((status) => {
    if (status === "rejected") {
      console.warn("Clé CARTO refusée (tuiles « API KEY REQUIRED ») : fonds de secours utilisés. "
        + "Vérifiez la clé et ses domaines autorisés dans le tableau de bord CARTO.");
    }
    try {
      if (status !== "unknown") sessionStorage.setItem(CARTO_CHECK_STORAGE, `${fingerprint}:${status}`);
    } catch {
      /* idem */
    }
    if (status === "unknown") cartoCheck = null; // vérification à refaire au prochain montage
    cartoListeners.forEach((listener) => listener(status));
    return status;
  });
  const timeout = new Promise<CartoKeyStatus>((resolve) => setTimeout(() => resolve("unknown"), CARTO_CHECK_TIMEOUT_MS));
  return Promise.race([verdict, timeout]);
}

function useCartoStatus(enabled: boolean): CartoKeyStatus {
  const [status, setStatus] = useState<CartoKeyStatus>(() =>
    (CARTO_CONFIGURED && enabled ? cachedCartoStatus() ?? "pending" : "unknown"));
  useEffect(() => {
    if (!enabled) return; // vue en véhicule : CARTO n'y figure pas, rien à vérifier
    if (!CARTO_CONFIGURED) {
      if (!cartoMissingWarned) {
        cartoMissingWarned = true;
        console.warn("Clé CARTO non configurée (NEXT_PUBLIC_CARTO_API_KEY) : fonds Plan et Sombre de secours.");
      }
      return;
    }
    let alive = true;
    const listener = (result: CartoKeyStatus) => { if (alive) setStatus(result); };
    cartoListeners.add(listener);
    if (status === "pending") {
      cartoCheck ??= startCartoCheck();
      cartoCheck.then(listener);
    }
    return () => { alive = false; cartoListeners.delete(listener); };
  }, [status, enabled]);
  return status;
}

/** Délai avant de retenter le fournisseur préféré après une bascule de secours. */
const RETRY_PREFERRED_MS = 5 * 60_000;

/** Fond de carte résilient : affiche le premier fournisseur de la chaîne du fond choisi et
 *  bascule sur le suivant si ses tuiles échouent en série (cf. `shouldFallback`). Une coupure
 *  RÉSEAU (hors ligne) n'est pas une panne du fournisseur ; après une bascule, le fournisseur
 *  préféré est retenté au retour du réseau et toutes les 5 minutes. Monté avec `key` : changer
 *  de fond repart du fournisseur préféré. Ses props ne dépendent QUE du fond choisi : une mise
 *  à jour des positions GPS ne recharge aucune tuile. */
function BaseTiles({ layers, layer, carto, onProvider }: {
  layers: Record<BaseLayerKey, BaseLayer>; layer: BaseLayerKey; carto: CartoKeyStatus;
  onProvider: (fallback: string | null) => void;
}) {
  const sources = useMemo(() => usableSources(layers[layer], carto), [layers, layer, carto]);
  const [index, setIndex] = useState(0);
  // Fournisseur courant, lu par les gestionnaires : une tuile tardive d'un fournisseur déjà
  // abandonné (requête partie avant la bascule) ne compte pas contre le suivant.
  const current = useRef(0);

  const source = sources[Math.min(index, sources.length - 1)];
  // Bandeau « secours » dès que le fond servi n'est pas le fournisseur prévu (panne, clé refusée).
  const preferred = layers[layer].sources[0];
  useEffect(() => {
    onProvider(source && preferred && source.id !== preferred.id ? source.provider : null);
  }, [source, preferred, onProvider]);

  // Retour au fournisseur préféré : au retour du réseau, puis toutes les 5 minutes.
  useEffect(() => {
    if (index === 0) return;
    const retry = () => { current.current = 0; setIndex(0); };
    const timer = window.setTimeout(retry, RETRY_PREFERRED_MS);
    window.addEventListener("online", retry);
    return () => { window.clearTimeout(timer); window.removeEventListener("online", retry); };
  }, [index]);

  // Compteurs PROPRES à chaque fournisseur (recréés à chaque bascule).
  const handlers = useMemo(() => {
    const mine = index;
    const counts = { errors: 0, loads: 0 };
    return {
      tileload: () => {
        if (current.current === mine) counts.loads += 1;
      },
      tileerror: () => {
        if (current.current !== mine) return;
        if (typeof navigator !== "undefined" && navigator.onLine === false) return; // hors ligne
        counts.errors += 1;
        if (shouldFallback(counts.errors, counts.loads) && mine + 1 < sources.length) {
          current.current = mine + 1;
          // Jamais l'URL dans la console : elle porte la clé du fournisseur.
          console.warn(`Fond de carte « ${sources[mine].provider} » indisponible : bascule vers « ${sources[mine + 1].provider} ».`);
          setIndex(mine + 1);
        }
      },
    };
  }, [index, sources]);

  if (!source) return null;
  return (
    <TileLayer
      key={`${layer}:${source.id}`}
      url={source.url}
      attribution={source.attribution}
      subdomains={source.subdomains ?? "abc"}
      maxNativeZoom={source.maxNativeZoom}
      maxZoom={MAP_MAX_ZOOM}
      referrerPolicy={source.referrerPolicy}
      eventHandlers={handlers}
    />
  );
}

const STATUS_COLOR: Record<string, string> = {
  available: "#10b981",
  reserved: "#8b5cf6",
  on_trip: "#0ea5e9",
  maintenance: "#f59e0b",
  out_of_service: "#f43f5e",
  unavailable: "#94a3b8",
};

// Icône voiture (Material) en blanc, sur pastille colorée par statut.
const CAR_SVG =
  '<svg viewBox="0 0 24 24" width="16" height="16" fill="#fff"><path d="M18.92 6.01C18.72 5.42 18.16 5 17.5 5h-11c-.66 0-1.21.42-1.42 1.01L3 12v8c0 .55.45 1 1 1h1c.55 0 1-.45 1-1v-1h12v1c0 .55.45 1 1 1h1c.55 0 1-.45 1-1v-8l-2.08-5.99zM6.5 16c-.83 0-1.5-.67-1.5-1.5S5.67 13 6.5 13s1.5.67 1.5 1.5S7.33 16 6.5 16zm11 0c-.83 0-1.5-.67-1.5-1.5s.67-1.5 1.5-1.5 1.5.67 1.5 1.5-.67 1.5-1.5 1.5zM5 11l1.5-4.5h11L19 11H5z"/></svg>';

function carIcon(p: VehiclePosition, active: boolean) {
  const color = STATUS_COLOR[p.status] ?? "#94a3b8";
  const label = p.driver_name || p.registration;
  const ring = active ? "box-shadow:0 0 0 4px rgba(249,115,22,.55);" : "box-shadow:0 2px 6px rgba(0,0,0,.35);";
  const pulse = p.is_late ? "animation:kxpulse 1.2s infinite;" : "";
  return L.divIcon({
    className: "kx-vehicle",
    html:
      `<div style="display:flex;flex-direction:column;align-items:center;">` +
      `<div style="display:flex;align-items:center;justify-content:center;width:30px;height:30px;border-radius:9999px;background:${color};border:2px solid #fff;${ring}${pulse}">${CAR_SVG}</div>` +
      `<span style="margin-top:3px;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;background:#0b1322;color:#fff;font-size:10px;font-weight:600;line-height:1;padding:3px 7px;border-radius:9999px;box-shadow:0 1px 3px rgba(0,0,0,.4);">${label}</span>` +
      `</div>`,
    iconSize: [120, 48],
    iconAnchor: [60, 16],
    tooltipAnchor: [0, -16],
  });
}

function FitBounds({ positions, fallback }: { positions: VehiclePosition[]; fallback?: [number, number][] }) {
  const map = useMap();
  const done = useRef(false);
  useEffect(() => {
    if (done.current) return;
    let pts = positions
      .filter((p) => p.latitude && p.longitude)
      .map((p) => [Number(p.latitude), Number(p.longitude)] as [number, number]);
    if (pts.length === 0 && fallback?.length) pts = fallback;
    try {
      if (pts.length === 1) {
        map.setView(pts[0], 14);
        done.current = true;
      } else if (pts.length > 1) {
        map.fitBounds(pts, { padding: [50, 50] });
        done.current = true;
      }
    } catch {
      /* carte non prête / démontée — ignoré */
    }
  }, [positions, map]);
  return null;
}

function FocusOnSelect({
  positions,
  selectedId,
}: {
  positions: VehiclePosition[];
  selectedId?: string | null;
}) {
  const map = useMap();
  useEffect(() => {
    if (!selectedId) return;
    const p = positions.find((x) => x.id === selectedId);
    if (p?.latitude && p?.longitude) {
      try {
        map.stop();
        map.flyTo([Number(p.latitude), Number(p.longitude)], 15, { duration: 0.8 });
      } catch {
        /* carte non prête / démontée — ignoré */
      }
    }
  }, [selectedId, positions, map]);
  return null;
}

function pinIcon(color: string, glyph: string) {
  return L.divIcon({
    className: "",
    html:
      `<div style="display:flex;align-items:center;justify-content:center;width:28px;height:28px;border-radius:9999px 9999px 9999px 2px;background:${color};border:2px solid #fff;box-shadow:0 2px 6px rgba(0,0,0,.4);transform:rotate(-45deg);">` +
      `<span style="transform:rotate(45deg);font-size:13px;">${glyph}</span></div>`,
    iconSize: [28, 28],
    iconAnchor: [14, 28],
  });
}
const ORIGIN_ICON = pinIcon("#10b981", "📍");
const REPLAY_ICON = L.divIcon({
  className: "",
  html:
    '<div style="display:flex;align-items:center;justify-content:center;width:20px;height:20px;' +
    'border-radius:9999px;background:#f97316;border:3px solid #fff;box-shadow:0 0 0 3px rgba(249,115,22,.35);"></div>',
  iconSize: [20, 20],
  iconAnchor: [10, 10],
});

function ClickHandler({ onMapClick }: { onMapClick: (lat: number, lng: number) => void }) {
  useMapEvents({ click: (e) => onMapClick(e.latlng.lat, e.latlng.lng) });
  return null;
}

function Recenter({ to }: { to?: [number, number] | null }) {
  const map = useMap();
  // `to` est souvent un nouveau tableau à chaque rendu (positions de suivi temps réel).
  // On ne recentre que lorsque les coordonnées changent réellement, et sans transition de
  // zoom : un `flyTo` interrompu (animation qui se chevauche ou carte démontée) provoque le
  // crash Leaflet « Cannot read properties of undefined (reading '_leaflet_pos') ».
  const lastKey = useRef<string>("");
  useEffect(() => {
    if (!to || to[0] == null || to[1] == null) return;
    const key = `${to[0].toFixed(5)},${to[1].toFixed(5)}`;
    if (key === lastKey.current) return;
    lastKey.current = key;
    try {
      map.stop(); // annule toute animation en cours avant de bouger
      map.setView(to, map.getZoom() || 14, { animate: true, duration: 0.6 });
    } catch {
      /* carte non prête / démontée — ignoré */
    }
  }, [to, map]);
  return null;
}

const FLAG_ICON = L.divIcon({
  className: "",
  html:
    '<div style="display:flex;flex-direction:column;align-items:center;">' +
    '<div style="display:flex;align-items:center;justify-content:center;width:30px;height:30px;border-radius:9999px 9999px 9999px 2px;background:#0f172a;border:2px solid #fff;box-shadow:0 2px 6px rgba(0,0,0,.4);transform:rotate(-45deg);">' +
    '<span style="transform:rotate(45deg);font-size:14px;">🏁</span></div></div>',
  iconSize: [30, 30],
  iconAnchor: [15, 30],
});

export default function MapView({
  positions,
  selectedId,
  onSelect,
  planned,
  actual,
  destination,
  zones,
  origin,
  onMapClick,
  recenterTo,
  fitTo,
  marker,
  inVehicle = false,
}: {
  positions: VehiclePosition[];
  selectedId?: string | null;
  onSelect?: (id: string) => void;
  planned?: [number, number][];
  actual?: [number, number][];
  destination?: [number, number] | null;
  /** Zones affichables : polygone dessiné OU centre + rayon (zone opérationnelle). */
  zones?: {
    id: string; name: string; zone_type: string; polygon: [number, number][];
    center?: [number, number] | null; radius_m?: number | null;
  }[];
  origin?: [number, number] | null;
  onMapClick?: (lat: number, lng: number) => void;
  recenterTo?: [number, number] | null;
  /** Cadrage de secours (ex. itinéraire) quand aucune position véhicule. */
  fitTo?: [number, number][];
  /** Marqueur ponctuel mobile (ex. position courante en relecture d'itinéraire). */
  marker?: [number, number] | null;
  /** Vue EN VÉHICULE (chauffeur, guidage en course) : fonds sans CARTO, dont les conditions
   *  interdisent la navigation en temps réel et l'affichage sur un véhicule en mouvement. */
  inVehicle?: boolean;
}) {
  const located = useMemo(
    () => positions.filter((p) => p.latitude && p.longitude),
    [positions],
  );
  const center: [number, number] = located.length
    ? [Number(located[0].latitude), Number(located[0].longitude)]
    : DEFAULT_CENTER;

  const [layer, setLayer] = useState<BaseLayerKey>("plan");
  const [fallbackProvider, setFallbackProvider] = useState<string | null>(null);
  const carto = useCartoStatus(!inVehicle);

  return (
    <>
      {/* Transition fluide des marqueurs + pulsation des retards */}
      <style>{`
        .leaflet-marker-icon.kx-vehicle { transition: transform 1.2s linear; }
        @keyframes kxpulse { 0%,100% { box-shadow:0 0 0 0 rgba(244,63,94,.6);} 50% { box-shadow:0 0 0 8px rgba(244,63,94,0);} }
      `}</style>

      {/* Sélecteur de fond de carte.
          Mobile : en haut à droite (le panneau de commande occupe le bas, le zoom Leaflet le haut-gauche).
          Desktop : en bas à gauche (le panneau est en haut à droite → coin libre). */}
      <div className="absolute right-3 top-3 z-[600] flex overflow-hidden rounded-lg border border-line bg-surface/95 shadow-lg backdrop-blur lg:bottom-6 lg:left-3 lg:right-auto lg:top-auto">
        {BASE_LAYER_KEYS.map((k) => (
          <button
            key={k}
            onClick={() => setLayer(k)}
            className={
              "px-2.5 py-1.5 text-[11px] font-medium transition-colors " +
              (layer === k ? "bg-brand-600 text-white" : "text-muted hover:bg-surface2")
            }
          >
            {BASE_LAYERS[k].label}
          </button>
        ))}
      </div>

      {fallbackProvider && (
        <div className="pointer-events-none absolute bottom-6 right-3 z-[600] rounded-md bg-amber-500/90 px-2 py-1 text-[10px] font-medium text-white shadow">
          Fond de secours : {fallbackProvider}
        </div>
      )}

      <MapContainer center={center} zoom={DEFAULT_ZOOM} maxZoom={MAP_MAX_ZOOM} className="h-full w-full"
                    style={{ background: "#0a1120" }}>
        <BaseTiles key={`${inVehicle ? "vehicle" : "office"}:${layer}:${carto}`}
                   layers={inVehicle ? VEHICLE_BASE_LAYERS : BASE_LAYERS} layer={layer} carto={carto}
                   onProvider={setFallbackProvider} />
        <FitBounds positions={located} fallback={fitTo} />
        <FocusOnSelect positions={located} selectedId={selectedId} />
        {onMapClick && <ClickHandler onMapClick={onMapClick} />}
        <Recenter to={recenterTo} />
        {origin && <Marker position={origin} icon={ORIGIN_ICON} />}
        {marker && <Marker position={marker} icon={REPLAY_ICON} />}

        {/* Zones : géofences dessinées (polygone) ET zones opérationnelles (centre + rayon).
            Une zone définie par un rayon n'a pas de polygone : sans ce second rendu, les
            zones de dispatching semées restaient invisibles sur la carte. */}
        {zones?.map((z) => {
          const forbidden = z.zone_type === "forbidden";
          const operational = z.zone_type === "operational";
          const color = forbidden ? "#f43f5e" : operational ? "#8b5cf6" : "#10b981";
          const style = {
            color, weight: 2, dashArray: "6 6", fillColor: color, fillOpacity: 0.07,
          };
          if (z.polygon?.length >= 3) {
            return (
              <Polygon key={z.id} positions={z.polygon} pathOptions={style}>
                <Tooltip sticky><span className="text-xs font-medium">{z.name}</span></Tooltip>
              </Polygon>
            );
          }
          if (z.center && z.radius_m) {
            return (
              <Circle key={z.id} center={z.center} radius={z.radius_m} pathOptions={style}>
                <Tooltip sticky><span className="text-xs font-medium">{z.name}</span></Tooltip>
              </Circle>
            );
          }
          return null;
        })}

        {/* Itinéraire prévu — style Google Maps (casing + ligne bleue) */}
        {planned && planned.length > 1 && (
          <>
            <Polyline positions={planned} pathOptions={{ color: "#1d4ed8", weight: 9, opacity: 0.9, lineCap: "round", lineJoin: "round" }} />
            <Polyline positions={planned} pathOptions={{ color: "#3b82f6", weight: 5, opacity: 1, lineCap: "round", lineJoin: "round" }} />
          </>
        )}
        {/* Trace réelle parcourue (orange) par-dessus la route */}
        {actual && actual.length > 1 && (
          <Polyline positions={actual} pathOptions={{ color: "#f97316", weight: 5, opacity: 0.95, lineCap: "round", lineJoin: "round" }} />
        )}
        {/* Marqueur de destination */}
        {destination && <Marker position={destination} icon={FLAG_ICON} />}
        {located.map((p) => (
          <Marker
            key={p.id}
            position={[Number(p.latitude), Number(p.longitude)]}
            icon={carIcon(p, p.id === selectedId)}
            zIndexOffset={p.id === selectedId ? 1000 : 0}
            eventHandlers={{ click: () => onSelect?.(p.id) }}
          >
            <Tooltip direction="top" offset={[0, -4]}>
              <span className="text-xs font-medium">
                {p.registration} — {p.status_display}
                {p.driver_name ? ` · ${p.driver_name}` : ""}
                {p.speed_kmh ? ` · ${p.speed_kmh} km/h` : ""}
              </span>
            </Tooltip>
          </Marker>
        ))}
      </MapContainer>
    </>
  );
}
