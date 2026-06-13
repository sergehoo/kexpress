"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { MapContainer, Marker, Polygon, Polyline, TileLayer, Tooltip, useMap, useMapEvents } from "react-leaflet";
import L from "leaflet";
import "leaflet/dist/leaflet.css";

import type { VehiclePosition } from "@/lib/types";

// Fonds de carte disponibles (Plan / Sombre / Satellite)
const BASE_LAYERS = {
  plan: {
    label: "Plan",
    url: "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
    attribution: "&copy; OpenStreetMap, &copy; CARTO",
  },
  dark: {
    label: "Sombre",
    url: "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
    attribution: "&copy; OpenStreetMap, &copy; CARTO",
  },
  satellite: {
    label: "Satellite",
    url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    attribution: "&copy; Esri, Maxar, Earthstar Geographics",
  },
} as const;
type LayerKey = keyof typeof BASE_LAYERS;

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
    if (pts.length === 1) {
      map.setView(pts[0], 14);
      done.current = true;
    } else if (pts.length > 1) {
      map.fitBounds(pts, { padding: [50, 50] });
      done.current = true;
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
      map.flyTo([Number(p.latitude), Number(p.longitude)], 15, { duration: 0.8 });
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
  useEffect(() => {
    if (to) map.flyTo(to, 14, { duration: 0.8 });
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
}: {
  positions: VehiclePosition[];
  selectedId?: string | null;
  onSelect?: (id: string) => void;
  planned?: [number, number][];
  actual?: [number, number][];
  destination?: [number, number] | null;
  zones?: { id: string; name: string; zone_type: string; polygon: [number, number][] }[];
  origin?: [number, number] | null;
  onMapClick?: (lat: number, lng: number) => void;
  recenterTo?: [number, number] | null;
  /** Cadrage de secours (ex. itinéraire) quand aucune position véhicule. */
  fitTo?: [number, number][];
  /** Marqueur ponctuel mobile (ex. position courante en relecture d'itinéraire). */
  marker?: [number, number] | null;
}) {
  const located = useMemo(
    () => positions.filter((p) => p.latitude && p.longitude),
    [positions],
  );
  const center: [number, number] = located.length
    ? [Number(located[0].latitude), Number(located[0].longitude)]
    : [5.345, -4.024];

  const [layer, setLayer] = useState<LayerKey>("plan");
  const base = BASE_LAYERS[layer];

  return (
    <>
      {/* Transition fluide des marqueurs + pulsation des retards */}
      <style>{`
        .leaflet-marker-icon.kx-vehicle { transition: transform 1.2s linear; }
        @keyframes kxpulse { 0%,100% { box-shadow:0 0 0 0 rgba(244,63,94,.6);} 50% { box-shadow:0 0 0 8px rgba(244,63,94,0);} }
      `}</style>

      {/* Sélecteur de fond de carte */}
      <div className="absolute bottom-6 left-3 z-[600] flex overflow-hidden rounded-lg border border-line bg-surface/95 shadow-lg backdrop-blur">
        {(Object.keys(BASE_LAYERS) as LayerKey[]).map((k) => (
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

      <MapContainer center={center} zoom={12} className="h-full w-full" style={{ background: "#0a1120" }}>
        <TileLayer key={layer} attribution={base.attribution} url={base.url} />
        <FitBounds positions={located} fallback={fitTo} />
        <FocusOnSelect positions={located} selectedId={selectedId} />
        {onMapClick && <ClickHandler onMapClick={onMapClick} />}
        <Recenter to={recenterTo} />
        {origin && <Marker position={origin} icon={ORIGIN_ICON} />}
        {marker && <Marker position={marker} icon={REPLAY_ICON} />}

        {/* Zones de géofencing */}
        {zones?.map((z) => {
          const forbidden = z.zone_type === "forbidden";
          const color = forbidden ? "#f43f5e" : "#10b981";
          return z.polygon?.length >= 3 ? (
            <Polygon
              key={z.id}
              positions={z.polygon}
              pathOptions={{ color, weight: 2, dashArray: "6 6", fillColor: color, fillOpacity: 0.07 }}
            >
              <Tooltip sticky><span className="text-xs font-medium">{z.name}</span></Tooltip>
            </Polygon>
          ) : null;
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
