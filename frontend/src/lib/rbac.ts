/** Organisation des vues par rôle (miroir de RoleChoices côté backend).
 *
 * La sécurité des DONNÉES reste garantie par l'API (scoping + permissions DRF) ;
 * cette matrice organise l'EXPÉRIENCE : navigation épurée par métier et
 * redirection hors des pages qui ne concernent pas le rôle.
 */

export const ALL_ROLES = [
  "super_admin", "company_admin", "subsidiary_admin", "fleet_manager",
  "department_manager", "requester", "driver", "finance", "auditor",
] as const;

export type Role = (typeof ALL_ROLES)[number];

const ADMINS: Role[] = ["super_admin", "company_admin", "subsidiary_admin"];
const MANAGERS: Role[] = [...ADMINS, "fleet_manager"];

/** Pages accessibles par rôle (préfixe de route → rôles autorisés). */
export const PAGE_ROLES: Record<string, Role[]> = {
  // Pilotage
  "/dashboard": [...MANAGERS, "department_manager", "finance", "auditor"],
  "/fleet-control": MANAGERS,
  // Centre de dispatching (§4) : exploitation de la flotte, pas consultation.
  "/dispatching": MANAGERS,
  "/map": [...MANAGERS, "department_manager", "requester", "driver"],
  // Exploitation
  "/driver": ["driver", ...ADMINS],
  "/reservations": [...MANAGERS, "department_manager", "requester", "auditor"],
  "/planning-vehicles": [...MANAGERS, "department_manager"],
  "/planning-drivers": MANAGERS,
  "/trips": [...MANAGERS, "department_manager", "requester", "driver", "auditor"],
  // Flotte
  "/vehicles": [...MANAGERS, "finance", "auditor"],
  "/drivers": MANAGERS,
  "/maintenance": [...MANAGERS, "finance"],
  // Finance
  // « Carburant » renommé « Énergie » (§12) ; `/fuel` redirige (cf. next.config.mjs) et
  // reste listé pour que la redirection ne soit pas coupée par le garde d'accès.
  "/energie": [...MANAGERS, "finance"],
  "/fuel": [...MANAGERS, "finance"],
  "/expenses": [...MANAGERS, "finance"],
  "/reports": [...MANAGERS, "finance", "auditor"],
  // Organisation
  "/subsidiaries": ADMINS,
  "/employees": ADMINS,
  "/incidents": [...MANAGERS, "driver", "auditor"],
  "/alerts": [...MANAGERS, "department_manager", "finance"],
  // Système
  "/notifications": [...ALL_ROLES],
  "/kbot": [...ALL_ROLES],
  "/audit": ["super_admin", "company_admin", "auditor"],
  "/settings": [...ALL_ROLES],
};

/** Rôles habilités à gérer l'affectation des courses (miroir de TRIP_START_MANAGER_ROLES). */
export function canManageFleet(role?: string | null): boolean {
  return MANAGERS.includes(role as Role);
}

/** Page d'accueil par rôle (atterrissage après connexion). */
export function homeFor(role: string): string {
  if (role === "requester" || role === "driver") return "/map";
  return "/dashboard";
}

export function canAccess(role: string, pathname: string): boolean {
  const entry = Object.entries(PAGE_ROLES).find(
    ([prefix]) => pathname === prefix || pathname.startsWith(prefix + "/"),
  );
  if (!entry) return true; // route non répertoriée : laissée à l'API
  return entry[1].includes(role as Role);
}
