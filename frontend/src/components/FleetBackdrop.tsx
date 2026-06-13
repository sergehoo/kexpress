/**
 * FleetBackdrop — décor animé de la page de connexion.
 *
 * Route fuyant vers un POINT DE FUITE CENTRAL (profondeur vers le fond) et flotte
 * roulant DANS LE SENS DE LA ROUTE, VUE DE FACE : chaque véhicule naît au point
 * de fuite (petit, transparent) puis s'avance vers le spectateur en grossissant
 * le long d'une voie. Les silhouettes sont dessinées de FACE (on voit l'avant :
 * pare-brise, phares, calandre) → les véhicules viennent vers le spectateur.
 *
 * Profondeur : lignes de fuite convergentes, marquages qui défilent du fond vers
 * l'avant, lueur d'horizon, et échelle/opacité croissant à l'approche.
 *
 * Contraintes : composant pur (aucun hook, aucun "use client", aucun import hors
 * types React), SVG inline uniquement, zéro asset/dépendance, opacités faibles
 * (décor derrière la carte), mobile-first sans débordement, animations
 * centralisées dans globals.css et figées (mais visibles) sous
 * prefers-reduced-motion.
 *
 * Dessin de face : viewBox 0 0 72 60, sol à y ≈ 58, silhouettes symétriques.
 * Pare-brise / phares / calandre sont creusés en réserve (couleur navy) pour
 * rester nets même à très faible opacité.
 */

// Réserve : couleur du fond navy, pour « creuser » vitres, phares, calandre.
const GROUND = "var(--color-navy-900)";

function Berline() {
  return (
    <g fill="currentColor">
      {/* Corps de face : ailes larges, pavillon plus étroit. */}
      <path d="M12 56V36q0-5 5-6l5-1 4-8q1-3 5-3h10q4 0 5 3l4 8 5 1q5 1 5 6v20Z" />
      {/* Pare-brise (réserve). */}
      <path d="M27 21h18l3 8H24Z" fill={GROUND} opacity={0.5} />
      {/* Phares (réserve). */}
      <rect x="16" y="39" width="9" height="5" rx="2.5" fill={GROUND} opacity={0.5} />
      <rect x="47" y="39" width="9" height="5" rx="2.5" fill={GROUND} opacity={0.5} />
      {/* Calandre (réserve). */}
      <rect x="30" y="45" width="12" height="4" rx="1.5" fill={GROUND} opacity={0.4} />
      {/* Roues avant (coins). */}
      <rect x="9" y="50" width="8" height="8" rx="2.5" />
      <rect x="55" y="50" width="8" height="8" rx="2.5" />
    </g>
  );
}

function Fourgon() {
  return (
    <g fill="currentColor">
      {/* Caisse haute de face. */}
      <path d="M13 56V22q0-5 5-5h36q5 0 5 5v34Z" />
      {/* Grand pare-brise (réserve). */}
      <rect x="19" y="22" width="34" height="11" rx="2" fill={GROUND} opacity={0.5} />
      {/* Phares (réserve). */}
      <rect x="17" y="44" width="9" height="5" rx="2" fill={GROUND} opacity={0.5} />
      <rect x="46" y="44" width="9" height="5" rx="2" fill={GROUND} opacity={0.5} />
      {/* Calandre (réserve). */}
      <rect x="29" y="49" width="14" height="4" rx="1" fill={GROUND} opacity={0.4} />
      {/* Roues. */}
      <rect x="10" y="50" width="9" height="8" rx="2.5" />
      <rect x="53" y="50" width="9" height="8" rx="2.5" />
    </g>
  );
}

function PickUp() {
  return (
    <g fill="currentColor">
      {/* Cabine de face, garde au sol haute. */}
      <path d="M11 56V29q0-4 4-5l5-1 3-6q1-3 5-3h16q4 0 5 3l3 6 5 1q4 1 4 5v27Z" />
      {/* Pare-brise (réserve). */}
      <path d="M24 19h24l3 9H21Z" fill={GROUND} opacity={0.5} />
      {/* Phares (réserve). */}
      <rect x="15" y="39" width="10" height="6" rx="2" fill={GROUND} opacity={0.5} />
      <rect x="47" y="39" width="10" height="6" rx="2" fill={GROUND} opacity={0.5} />
      {/* Calandre (réserve). */}
      <rect x="29" y="47" width="14" height="5" rx="1" fill={GROUND} opacity={0.4} />
      {/* Roues (larges). */}
      <rect x="7" y="49" width="10" height="9" rx="2.5" />
      <rect x="55" y="49" width="10" height="9" rx="2.5" />
    </g>
  );
}

function Camion() {
  return (
    <g fill="currentColor">
      {/* Face plate et haute (caisse de livraison). */}
      <path d="M12 56V13q0-3 3-3h42q3 0 3 3v43Z" />
      {/* Pare-brise large (réserve). */}
      <rect x="17" y="15" width="38" height="12" rx="2" fill={GROUND} opacity={0.5} />
      {/* Phares (réserve). */}
      <rect x="16" y="44" width="10" height="6" rx="2" fill={GROUND} opacity={0.5} />
      <rect x="46" y="44" width="10" height="6" rx="2" fill={GROUND} opacity={0.5} />
      {/* Calandre / pare-chocs (réserve). */}
      <rect x="28" y="50" width="16" height="3" rx="1" fill={GROUND} opacity={0.4} />
      {/* Roues. */}
      <rect x="9" y="50" width="9" height="8" rx="2" />
      <rect x="54" y="50" width="9" height="8" rx="2" />
    </g>
  );
}

function MotoTaxi() {
  return (
    <g fill="currentColor">
      {/* Roue avant (centrale). */}
      <rect x="32" y="42" width="8" height="16" rx="4" />
      {/* Carénage / réservoir. */}
      <path d="M30 45q-2-12 6-15 8 3 6 15Z" />
      {/* Guidon. */}
      <path d="M24 27h24" fill="none" stroke="currentColor" strokeWidth={3} strokeLinecap="round" />
      {/* Phare (réserve). */}
      <circle cx="36" cy="35" r="3.5" fill={GROUND} opacity={0.5} />
    </g>
  );
}

const SHAPES = {
  fourgon: Fourgon,
  berline: Berline,
  pickup: PickUp,
  camion: Camion,
  moto: MotoTaxi,
} as const;

type Kind = keyof typeof SHAPES;

function Car({ kind, className }: { kind: Kind; className: string }) {
  const Shape = SHAPES[kind];
  return (
    <svg viewBox="0 0 72 60" className={className}>
      <Shape />
    </svg>
  );
}

type Lane = {
  show: string; // visibilité responsive (mobile → grand écran)
  x: number; // cible avant-plan, en vw depuis le point de fuite
  y: number; // cible avant-plan, en vh depuis le point de fuite
  s1: number; // échelle à l'avant-plan
  op: number; // opacité de croisière
  dur: number; // durée d'un passage (s)
  carClass: string; // largeur + teinte
  kinds: Kind[]; // véhicules échelonnés sur la voie
};

// Voies fuyant du centre : profondeur par l'échelle (s0 → s1) et la durée.
const S0 = 0.1; // échelle de naissance au point de fuite
const LANES: Lane[] = [
  // Voie proche droite — visible dès le mobile.
  {
    show: "block",
    x: 22,
    y: 42,
    s1: 1.0,
    op: 0.12,
    dur: 8,
    carClass: "w-32 text-white",
    kinds: ["pickup", "berline", "fourgon"],
  },
  // Voie proche gauche — dès sm.
  {
    show: "hidden sm:block",
    x: -24,
    y: 43,
    s1: 1.0,
    op: 0.12,
    dur: 8.6,
    carClass: "w-32 text-white",
    kinds: ["berline", "fourgon", "pickup"],
  },
  // Voie lointaine droite — teinte brand, dès lg.
  {
    show: "hidden lg:block",
    x: 44,
    y: 34,
    s1: 0.8,
    op: 0.09,
    dur: 10.5,
    carClass: "w-28 text-brand-400",
    kinds: ["camion", "berline", "moto"],
  },
  // Voie lointaine gauche — dès lg.
  {
    show: "hidden lg:block",
    x: -44,
    y: 34,
    s1: 0.8,
    op: 0.09,
    dur: 11,
    carClass: "w-28 text-white",
    kinds: ["fourgon", "pickup", "berline"],
  },
];

export default function FleetBackdrop() {
  return (
    <div aria-hidden="true" className="pointer-events-none absolute inset-0 overflow-hidden">
      {/* Lueur d'horizon chaude au point de fuite — profondeur atmosphérique. */}
      <div className="absolute left-1/2 top-[42%] h-40 w-[60%] -translate-x-1/2 -translate-y-1/2 rounded-[50%] bg-brand-500/[0.07] blur-[80px]" />

      {/* Sol fuyant : lignes convergentes + marquages défilant vers l'avant. */}
      <svg
        viewBox="0 0 100 58"
        preserveAspectRatio="none"
        className="absolute inset-x-0 bottom-0 h-[58%] w-full"
      >
        {/* Lignes de fuite vers le point de fuite (50, 0). */}
        <g stroke="white" strokeWidth="0.1" opacity={0.05}>
          <line x1="-25" y1="58" x2="50" y2="0" />
          <line x1="10" y1="58" x2="50" y2="0" />
          <line x1="35" y1="58" x2="50" y2="0" />
          <line x1="65" y1="58" x2="50" y2="0" />
          <line x1="90" y1="58" x2="50" y2="0" />
          <line x1="125" y1="58" x2="50" y2="0" />
        </g>
        {/* Marquages de voie défilant du fond vers le spectateur (sensation de route). */}
        <g stroke="white" strokeWidth="0.5" opacity={0.08} strokeLinecap="round">
          <line className="kx-road" x1="33" y1="58" x2="49" y2="1" strokeDasharray="2.5 5" />
          <line className="kx-road" x1="67" y1="58" x2="51" y2="1" strokeDasharray="2.5 5" />
        </g>
      </svg>

      {/* Flotte approchant du point de fuite. Chaque véhicule est ancré au point
          de fuite (left-1/2 top-[42%]) puis animé vers l'avant-plan. */}
      {LANES.map((lane, li) => (
        <div key={li} className={`absolute inset-0 ${lane.show}`}>
          {lane.kinds.map((kind, ci) => {
            // Décalages négatifs échelonnés (jamais 0 → toujours visible figé).
            const delay = -(lane.dur * (ci + 0.5)) / lane.kinds.length;
            const style = {
              "--x1": `${lane.x}vw`,
              "--y1": `${lane.y}vh`,
              "--s0": String(S0),
              "--s1": String(lane.s1),
              "--op": String(lane.op),
              animationDuration: `${lane.dur}s`,
              animationDelay: `${delay}s`,
            } as React.CSSProperties;
            return (
              <div
                key={ci}
                className="absolute left-1/2 top-[42%] -translate-x-1/2 -translate-y-1/2"
              >
                <div className="kx-approach" style={style}>
                  <Car kind={kind} className={lane.carClass} />
                </div>
              </div>
            );
          })}
        </div>
      ))}
    </div>
  );
}
