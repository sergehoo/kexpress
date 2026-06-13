# Kaydan Express — Backend

Plateforme de gestion de flotte multi-filiales (Django + DRF). **Phase 1 : fondation backend.**

## Stack (Phase 1)

- Python 3.14 · Django 6 · Django REST Framework
- PostgreSQL 16 (local) · SimpleJWT · drf-spectacular (OpenAPI)
- *Différé aux phases suivantes :* Next.js PWA, Channels/WebSocket, Celery/Redis,
  PostGIS, Docker, IA, exports PDF/Excel.

## Architecture

- **Multi-filiales** : `apps/core` fournit `TimeStampedModel` (PK UUID + horodatage)
  et `TenantScopedModel` (FK `subsidiary` + `TenantManager.for_user()` qui filtre par périmètre).
- **Isolation** : `TenantScopedViewSetMixin` restreint chaque queryset au périmètre de
  l'utilisateur. Les rôles à périmètre entreprise (super admin, admin entreprise, auditeur)
  voient toutes les filiales ; les autres uniquement la leur ; un employé demandeur ne voit
  que ses propres réservations.
- **Rôles** : champ `role` sur `accounts.User` + un groupe Django par rôle
  (commande `setup_roles`).

### Apps

| App | Rôle |
|-----|------|
| `core` | bases abstraites, enums, permissions, pagination, mixins |
| `accounts` | User custom (login email), rôles, auth JWT |
| `organizations` | Company → Subsidiary → Department |
| `vehicles` | véhicules, documents, historique de statut |
| `drivers` | chauffeurs, disponibilités, évaluations, incidents |
| `reservations` | réservations + workflow de validation configurable |
| `trips` | courses (exécution réelle), remise/retour, incidents, photos |
| `maintenance` | types, échéances, interventions |
| `expenses` | carburant, dépenses, budget flotte |
| `tracking` | GPS, sessions, itinéraires, géofencing, sync offline |
| `notifications` | notifications + préférences de canal |
| `audit` | journal d'audit générique |

> Les modèles GPS utilisent des coordonnées `Decimal` en Phase 1 ; migration vers
> PostGIS `PointField` prévue à la phase temps réel.

## Démarrage

PostgreSQL doit tourner. En dev local, l'instance Homebrew `postgresql@16` est lancée
sur le **port 5433** (auth `trust`) :

```bash
/opt/homebrew/opt/postgresql@16/bin/pg_ctl -D /opt/homebrew/var/postgresql@16 -o "-p 5433" start
```

Puis :

```bash
cp .env.example .env                      # adapter si besoin (DATABASE_URL)
.venv/bin/pip install -r requirements/local.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py setup_roles    # crée les groupes de rôles
.venv/bin/python manage.py seed_demo      # données de démonstration
.venv/bin/python manage.py runserver
```

## Comptes de démonstration

Tous avec le mot de passe **`demo1234`** :

| Email | Rôle | Périmètre |
|-------|------|-----------|
| `super@kaydan.test` | Super admin | global (accès `/admin/`) |
| `admin@kaydan.test` | Admin entreprise | toutes filiales |
| `admin.abj@kaydan.test` | Admin filiale | Abidjan |
| `flotte.abj@kaydan.test` | Gestionnaire flotte | Abidjan |
| `resp.abj@kaydan.test` | Responsable service | Abidjan |
| `employe.abj@kaydan.test` | Employé demandeur | Abidjan (ses demandes) |
| `chauffeur.abj@kaydan.test` | Chauffeur | Abidjan |
| `finance@kaydan.test` | Finance | global |
| `audit@kaydan.test` | Auditeur | global (lecture) |

## Endpoints clés

- `POST /api/auth/token/` · `POST /api/auth/refresh/` · `POST /api/auth/verify/`
- `GET  /api/auth/me/` — profil + rôle + périmètre
- `GET/POST /api/vehicles/` · `GET/POST /api/reservations/` · `GET /api/trips/` (scoping automatique)
- `GET  /api/schema/` (OpenAPI) · `GET /api/docs/` (Swagger UI)
- `GET  /admin/` (administration Django, tous les modèles enregistrés)

### Workflow réservation → course (Phase 2)

Actions `POST` sur `/api/reservations/{id}/` :
`submit` · `approve` · `reject` · `cancel` · `assign-vehicle` · `assign-driver`.

Actions `POST` sur `/api/trips/{id}/` : `start` · `end` · `close`.

Machine à états (statuts de réservation) :

```
DRAFT → (submit) → PENDING_MANAGER → (approve) → PENDING_FLEET → (approve) → APPROVED
      → (assign-vehicle) → VEHICLE_ASSIGNED → (assign-driver) → DRIVER_ASSIGNED
      → (trip.start) → IN_PROGRESS → (trip.end) → COMPLETED → (trip.close) → CLOSED
      reject → REJECTED   |   cancel → CANCELLED
```

Contrôles automatiques à l'affectation (§4) : cohérence de durée, capacité du véhicule,
disponibilité véhicule/chauffeur, **conflits horaires** (chevauchement avec une autre
réservation active), isolation par filiale. Chaque transition crée des notifications
internes et une entrée de **journal d'audit**, et met à jour le statut du véhicule
(disponible → réservé → en course → disponible).

Le workflow de validation est **configurable** via `ApprovalWorkflow`/`ApprovalStep`
(par filiale ou global) ; à défaut, l'enchaînement responsable → flotte s'applique.

## Tests

```bash
.venv/bin/python -m pytest tests/
```

Couvre l'isolation multi-filiales (manager + API) et l'endpoint `/me`.

## Phases

- ✅ **Phase 1** — fondation backend : modèles, multi-filiales, rôles, auth JWT, admin, OpenAPI.
- ✅ **Phase 2** — logique métier : workflow de validation configurable, affectation
  véhicule/chauffeur avec contrôles (conflits, capacité, disponibilité), cycle de vie des
  courses (départ/retour/clôture), notifications internes, journal d'audit.
- ✅ **Phase 3** — frontend Next.js PWA (`frontend/`) : auth JWT, dashboard, véhicules,
  réservations avec actions de workflow, courses, responsive mobile-first, installable + offline.
- ✅ **Phase 4** — UI premium : thème **orange / bleu nuit** + **mode clair/sombre**, shell
  premium (sidebar groupée à icônes, topbar avec recherche globale, sélecteur de filiale,
  notifications, profil), **dashboard intelligent** (8 KPIs, graphiques Recharts, widgets IA :
  résumé du jour, alertes, score santé flotte), assistant **K-BOT** flottant ancré sur les
  données, et toutes les pages de navigation (24 routes). Nouveaux endpoints backend :
  `/api/dashboard/stats/`, `/api/kbot/ask/`, `/api/subsidiaries/`, `/api/notifications/`.

- ✅ **Phase 5** — Centre de contrôle & données réelles : cartes **Leaflet** (Fleet Control
  Center avec liste + filtres + détail, et Carte temps réel), endpoint positions GPS scopé
  (`/api/tracking/positions/`) + positions de démo, pages **réelles** Chauffeurs / Maintenance /
  Carburant (API `/api/drivers/`, `/api/maintenance/`, `/api/fuel/`, `/api/expenses/`), et
  **K-BOT sur LLM réel** activable par `ANTHROPIC_API_KEY` (RAG ancré sur les données
  autorisées ; repli heuristique automatique sans clé).

- ✅ **Phase 6** — Temps réel WebSocket : stack **Django Channels + Redis** (ASGI/daphne),
  consumer `FleetConsumer` authentifié JWT qui **pousse les positions** (toutes les 4 s, scopé
  filiale) → remplace le polling REST (repli REST conservé). Icônes véhicule + nom du chauffeur
  + focus/zoom au clic.
  **Itinéraire façon Google Maps** : routage routier **OSRM** (suivi des routes réelles, cache
  `TripRoute.geometry`), le véhicule **progresse le long du tracé**, polyline *prévu* (route
  bleue + casing) vs *réel* (trace orange parcourue), **marqueur de destination** 🏁, carte
  d'info **distance / durée / progression / vitesse** (endpoint `/api/tracking/trips/<id>/route/`).
  Configurable via `OSRM_URL`.

- ✅ **Phase 7** — Pages de données réelles : **Filiales**, **Employés**, **Dépenses**,
  **Alertes** (agrégées : expirations assurance/visite/permis, maintenance due, retards),
  **Journal d'audit** (réservé périmètre entreprise/auditeur). Nouveaux endpoints :
  `/api/employees/`, `/api/audit/`, `/api/alerts/` (+ `/api/expenses/`). Seed enrichi
  (documents véhicule, échéances, dépenses, permis).

- ✅ **Phase 8** — Planning **FullCalendar** (véhicules & chauffeurs, événements depuis les
  réservations, couleurs par statut, vues semaine/mois/liste, thème clair/sombre), page
  **Incidents** (endpoint `/api/incidents/` agrégeant incidents course + chauffeur), page
  **Paramètres** (profil, thème, sécurité/périmètre, infos app).

- ✅ **Phase 9** — **Rapports exportables** : endpoint `/api/reports/export/?type=&fmt=`
  (parc, dépenses, maintenance, courses) en **CSV** (UTF-8 BOM), **Excel** (openpyxl, en-têtes
  stylés) et **PDF** (reportlab, tableau paysage). Scoping par périmètre + filiale sélectionnée,
  export **journalisé en audit**. Page Rapports avec téléchargements authentifiés (blob).
  **Toutes les pages de l'application sont désormais réelles.**

- ✅ **Phase 10** — **CRUD complet** sur les entités : infrastructure réutilisable côté front
  (`useCrud`, `EntityForm` piloté par field-spec, `RowActions` créer/éditer/supprimer + confirmation),
  endpoints backend **écrivables** et scopés (Véhicules, Chauffeurs, Maintenance, Carburant,
  Dépenses, Filiales, Employés). Permissions : écriture filiales/employés réservée aux
  administrateurs ; remplissage automatique de la filiale pour les rôles mono-filiale ;
  suppression d'employé = désactivation (préserve l'historique). Référentiel `maintenance-types`.

- ✅ **Phase 11** — **Vue `/map` type Uber** : carte plein écran + panneau de réservation
  (latéral desktop / bottom-sheet mobile). Recherche de lieux **autocomplete** (Nominatim +
  filiales), **« Utiliser ma position »** (géoloc), **clic carte** pour la destination,
  **estimation auto** (OSRM : distance / durée / **coût** + véhicules disponibles), **véhicules
  proches** triés par distance/ETA, marqueurs départ/destination + itinéraire tracé, véhicules
  live (WebSocket), **réservation directe** (Réserver maintenant / Planifier). Endpoints :
  `/api/places/search/`, `/api/routes/estimate/`, `/api/map/nearby-vehicles/`,
  `/api/reservations/from-map/`. MapView étendu (clic, marqueur origine, recentrage géoloc).

- ✅ **Phase 12** — **Suivi temps réel par course** sur `/map` : WebSocket
  `ws/trips/<id>/tracking/` (consumer authentifié JWT) qui pousse la position du véhicule
  affecté, l'état de la course, l'ETA et l'itinéraire (prévu + trace réelle) toutes les 4 s.
  Endpoint `/api/trips/active/`. La page `/map` **bascule automatiquement en mode suivi** quand
  l'utilisateur a une course en cours : panneau ETA + barre de progression, véhicule animé qui
  suit la route, chauffeur, **suivi d'étapes** (chauffeur affecté → en route → en cours →
  arrivée), recentrage automatique. Le mode flotte est désactivé en suivi (pas de double avance).

- ✅ **Phase 13** — K-BOT intégré à `/map` : **suggestion ancrée** dans
  `/api/map/nearby-vehicles/` (meilleur véhicule + distance + ETA + niveau de disponibilité,
  messages adaptés si aucun véhicule / pas de GPS) affichée en carte « Suggestion K-BOT » dans
  le panneau de réservation ; **destinations récentes** (localStorage, 5 dernières) proposées
  au focus des champs départ/destination.

- ✅ **Phase 14** — Identité visuelle & infrastructure :
  - **Logo Kaydan Express** intégré (sidebar, login, favicon, icônes PWA régénérées depuis l'emblème).
  - **Celery + beat** (Redis) : tâches périodiques `check_expirations` (documents/permis/maintenances,
    2×/jour) et `check_late_trips` (5 min), anti-spam 24 h. Lancer : `celery -A config worker -B`.
  - **Web Push réel (VAPID)** : clés dans `.env`, modèle `PushSubscription`, endpoints
    `/api/push/vapid-key/` et `/api/push/subscribe/`, envoi best-effort dans `notify()`
    (purge des abonnements expirés), bouton « Activer » dans Paramètres (SW déjà prêt).
  - **Géofencing temps réel** : `point_in_polygon` au tick GPS → `GeofenceAlert` sur transition
    (entrée/sortie), notifications critiques aux gestionnaires, zones dessinées sur la carte
    du Centre de contrôle (`/api/tracking/zones/`), alertes intégrées à `/api/alerts/`.
  - **Offline IndexedDB** : outbox `kx-offline` — réservation créée hors ligne mise en file,
    **synchronisation automatique** au retour réseau (composant `OfflineSync` + indicateur).

- ✅ **Phase 15** — Fin de course depuis la carte : boutons **« Terminer la course »**
  (km de retour optionnel — estimation automatique depuis la progression de l'itinéraire)
  et **« Clôturer la course »** directement dans le panneau de suivi de `/map`.
  Une course « Retour effectué » reste active sur la carte jusqu'à clôture
  (`/api/trips/active/` inclut `returned`), puis la carte revient au mode réservation.

- ✅ **Phase 16** — Page Réservations refondue : **statistiques cliquables** par phase
  (Brouillons / À valider / À affecter / En cours / Terminées), **recherche** multi-champs,
  bascule **Liste / Kanban** (colonnes par phase avec actions de workflow dans les cartes),
  cartes enrichies (accent coloré par statut, badge priorité, chips véhicule/chauffeur
  affectés, **mini-timeline des validations** responsable/flotte). Serializer enrichi
  (`vehicle_registration`, `driver_name`).

- ✅ **Phase 17** — Centre de contrôle « poste d'exploitation » :
  - Onglets **Véhicules / Demandes** dans le panneau latéral : les demandes à traiter
    (validation + affectation) sont gérées **sans quitter la carte** — Valider / Refuser /
    Affecter véhicule / Affecter chauffeur / « Prête au départ » (modales partagées
    `components/reservation-modals.tsx`, réutilisées par la page Réservations).
  - **Statistiques cliquables** (En course / Disponibles / En retard / Maintenance) qui
    filtrent la liste et la carte.
  - Cartographie : **sélecteur de fond de carte** Plan / Sombre / **Satellite** (CARTO + Esri),
    bouton **Suivre** (la carte reste centrée sur le véhicule sélectionné à chaque tick GPS).

- ✅ **Phase 18** — Flotte mutualisée & filiale déduite :
  - **Aucune restriction de filiale** sur l'exploitation : véhicules et chauffeurs sont
    visibles et affectables par toutes les filiales (`FleetWideManager` sur Vehicle/Driver,
    contrôles de filiale supprimés du workflow d'affectation). La filiale reste un simple
    rattachement administratif.
  - **`/map`** : plus de champ filiale — déduite du demandeur (repli : 1re filiale active
    pour les comptes entreprise).
  - **`/reservations`** : le sélecteur de filiale est remplacé par une **autocomplétion
    « Employé demandeur »** (recherche serveur) ; la filiale de la demande est déduite de
    l'employé sélectionné.

- ✅ **Phase 19** — Adresses **OpenStreetMap/Nominatim partout** : composant partagé
  `PlaceSearch` (suggestions pendant la saisie, **priorité Côte d'Ivoire** via
  `countrycodes=ci` + complément mondial, filiales internes, destinations récentes,
  texte libre accepté). Branché sur `/map` (départ + destination) et sur le champ
  **Destination** du formulaire de réservation. Pays prioritaire configurable
  (`PLACES_PRIORITY_COUNTRIES`).

- ✅ **Phase 20** — Géocodage inverse : endpoint `/api/places/reverse/` (Nominatim).
  Sur `/map`, « **Utiliser ma position actuelle** » remplit le champ Point de départ avec
  **l'adresse réelle** de l'utilisateur (état « Localisation en cours… », repli libellé
  générique hors ligne) ; le **clic sur la carte** géocode aussi l'adresse de destination.
  `PlaceSearch` accepte une valeur poussée par le parent (`externalValue`).

- ✅ **Phase 21** — **Fuel Intelligence** (le coût disparaît de l'expérience employé) :
  - L'estimation de trajet affiche **Distance / Temps / Consommation (L)** + niveau d'impact
    énergétique — **plus aucun coût** pour l'employé demandeur.
  - **Moteur apprenant** (`apps/fuelintel`) : profils de consommation L/100 km recalculés
    périodiquement (Celery, 6 h) depuis les courses réelles, à 5 niveaux (véhicule, chauffeur,
    type, filiale, flotte) avec lissage bayésien ; ajustement contextuel (urbain/route,
    heures de pointe). Plus la plateforme est utilisée, plus l'estimation est fiable.
  - **Prix carburant CI** (Super / Gasoil) : modèle `FuelPrice` avec historique des variations,
    mise à jour quotidienne configurable (source JSON distante `FUEL_PRICE_SOURCE_URL` ou
    valeurs réglementées en repli).
  - **Visibilité par rôle** : gestionnaires/admins/finance voient coût, estimé vs réel, écart,
    score d'efficacité (`fuel_intel` sur les courses) ; employés non (`403` sur fuel-intel).
  - **Fleet Fuel Intelligence** : onglet **Carburant** au Centre de contrôle (conso jour/mois,
    coûts, prévision mensuelle, écart prévu/réel, top consommateurs, chauffeurs économes,
    alertes de surconsommation, prix CI + historique) via `/api/fuel-intel/`.
  - **K-BOT énergie** : chauffeurs les plus économes, filiales sobres, véhicules en
    surconsommation / à remplacer (ancré sur les profils appris).

- ✅ **Phase 22** — **Vues détail commande & course** :
  - `/reservations/[id]` : trajet demandé complet (départ/destination/horaires/passagers/motif),
    fiche demandeur (nom, email, filiale), affectations véhicule/chauffeur, **circuit de
    validation** détaillé (validateur, décision, commentaire, horodatage), actions du workflow
    contextuelles, lien direct vers la course créée (`trip_id`).
  - `/trips/[id]` : **carte de l'itinéraire** (prévu/réel, progression, vitesse), exécution
    réelle (départ/retour, km, distance), participants, **bloc Carburant & énergie**
    (estimé/réel pour tous ; écart, score et coût réservés aux gestionnaires), incidents de
    course, actions démarrer/terminer/clôturer, lien retour vers la commande.
  - Navigation : titres cliquables + bouton « Détails » sur les listes Réservations et
    Courses, et dans l'onglet Demandes du Centre de contrôle.

- ✅ **Phase 23** — **Tracking 100 % données réelles** (fin de la simulation) :
  - Le simulateur serveur (avancement fictif le long de la route + vitesses aléatoires
    28-52 km/h) est **supprimé** : la lecture des positions est strictement passive.
  - **Ingestion GPS réelle** : `POST /api/tracking/trips/{id}/position/` — l'appareil d'un
    participant (demandeur, chauffeur, gestionnaire) pousse sa position pendant la course ;
    vitesse fournie par le capteur ou dérivée de l'intervalle réel entre deux points.
  - Côté PWA, `useGpsTracker` (watchPosition) émet automatiquement pendant une course en
    cours sur `/map` et sur le détail de course, avec indicateur d'état GPS.
  - **Calculs honnêtes** : distance parcourue = somme des segments GPS réels ; progression
    = parcouru/itinéraire ; vitesse affichée uniquement si la position date de < 3 min
    (sinon « — ») ; **sauts GPS filtrés** (vitesse implicite > 160 km/h exclue du cumul).
  - `end_trip` sans kilométrage saisi s'appuie sur la **distance GPS réelle** (repli :
    distance routière planifiée) ; les sessions de tracking sont figées à la fin de course
    (distance totale + vitesse moyenne réelles). Géofencing déclenché à l'ingestion.
  - 6 tests dédiés (lecture passive, dérivation de vitesse, anti-spam, saut GPS,
    vitesse périmée, participant requis).

- ✅ **Phase 24** — **Dashboard décisionnel + maintenance avancée + conformité flotte** :
  - **Dashboard décisionnel** (`/api/dashboard/stats/` v2) : filtres **période**
    (semaine/mois/année/personnalisée), **statut de réservation**, filiale ; KPIs
    réservations (total, validées, rejetées, annulées, en attente, taux, **temps moyen de
    traitement**), exploitation (km, courses, **temps moyen d'utilisation**, retards,
    incidents), carburant (estimé vs réel, écart, coûts) ; **Coût total flotte =
    dépenses générales + maintenance + carburant des courses** avec détail en 10 lignes ;
    courbes (réservations, validées/rejetées/annulées, carburant L+coût, km) ;
    **évolution des coûts par filiale** ; tops véhicules/courses coûteux. Les stats
    « nombre de véhicules par filiale » sont retirées.
  - **Imputation des charges par filiale** : une dépense, un plein ou une maintenance
    liés à une course sont automatiquement rattachés à la **filiale de la course**
    (`save()` sur MaintenanceRecord/Expense/FuelLog) — testé (crevaison → filiale course).
  - **Maintenance enrichie** : nature (préventive/corrective/urgente/périodique),
    **nomenclature de pannes configurable** (`BreakdownType`, 14 entrées), course liée,
    dates déclaration/immobilisation début-fin + **durée d'indisponibilité**, coûts
    **main-d'œuvre + pièces** (total auto), garage, justificatif/photo, responsable de
    validation ; 15 types de maintenance seedés ; KPIs maintenance au dashboard
    (préventive vs corrective, top pannes, indisponibilité par véhicule, taux
    d'immobilisation, annulations pour panne).
  - **Conformité véhicules** : modèles `InsurancePolicy`, `TechnicalInspection`,
    `VehicleRevision` (révision tous les **10 000 km** : prochaine = dernière + 10 000) ;
    badge Conforme/Non conforme + raisons + échéances sur la page Véhicules ; un véhicule
    **non conforme est bloqué à l'affectation** (raison affichée + véhicule conforme
    suggéré, option désactivée dans la modale) ; **rappels Celery** par paliers
    (J-30/15/7/0 + après expiration ; 2 000/1 000/500/0 km + dépassement) avec
    anti-doublon par palier ; KPIs conformité (taux, renouvellements, coûts annuels).

- ✅ **Phase 25** — **Fiche véhicule + alertes conformité partout + email** :
  - **Fiche véhicule** `/vehicles/[id]` : bandeau conformité (statut, raisons, échéances
    assurance/visite/révision), infos générales, **saisie + historique** des assurances,
    visites techniques et révisions (formulaires « Ajouter »), maintenances du véhicule.
    Cycle vérifié : révision saisie au kilométrage courant → véhicule **redevient
    conforme** instantanément (prochain seuil +10 000 km).
  - **Alertes conformité dans le flux global** `/api/alerts/` (types insurance /
    inspection / revision) → visibles page Alertes, dashboard et centre de contrôle.
  - **Canal email** sur `notify()` (meilleur effort) : activable via
    `NOTIFY_EMAIL_ENABLED` + `EMAIL_*`/`DEFAULT_FROM_EMAIL` en environnement
    (backend console en local, SMTP en production). SMS/WhatsApp restent optionnels.

- ✅ **Phase 26** — **Vues organisées par rôle** (`frontend/src/lib/rbac.ts`) :
  - Matrice rôle → pages (miroir de `RoleChoices`) : la sidebar n'affiche que les modules
    du métier ; badge du rôle dans la sidebar ; **garde de route** (une URL hors périmètre
    redirige vers l'accueil du rôle) ; **atterrissage par rôle** après connexion
    (employé/chauffeur → Carte temps réel, gestion → Tableau de bord).
  - Périmètres : admins (tout) · gestionnaire flotte (pilotage + exploitation + flotte +
    finance + incidents/alertes) · responsable de service (dashboard, carte, réservations,
    planning véhicules, courses, alertes) · **employé** (carte, réservations, courses) ·
    **chauffeur** (carte, courses, incidents) · **finance** (dashboard, véhicules,
    maintenance, carburant, dépenses, rapports, alertes) · **auditeur** (dashboard,
    réservations, courses, véhicules, rapports, incidents, **journal d'audit**).
  - La sécurité des données reste assurée côté API (scoping/permissions DRF) — le RBAC
    frontend organise l'expérience.

- ✅ **Phase 27** — **Statistiques de liste + vues détail + exports périodiques** :
  - Composant `StatChips` réutilisable : rangée de KPIs en tête de **chaque page de liste**
    (plannings, courses, véhicules, chauffeurs, maintenance, dépenses).
  - **Plannings véhicules & chauffeurs** : stats (planifiées, en cours, taux de
    planification/charge) + **détail au clic sur un créneau** (modale + lien fiche).
  - **Courses** : courses affichées, en cours, terminées, km cumulés, carburant réel.
  - **Véhicules** : disponibles, en course, maintenance, **taux de conformité**.
  - **Chauffeurs** : disponibles, permis ≤ 30 j, note moyenne + **fiche chauffeur**
    `/drivers/[id]` (coordonnées, permis avec alerte d'expiration, stats d'activité
    réelles — courses, km, carburant, L/100 km — et historique des courses).
  - **Dépenses** : montant total/moyen, top catégorie, liées à un véhicule + **détail au
    clic sur une ligne** + champ course liée (imputation filiale auto).
  - **Rapports** : **filtre périodique d'export** (Tout / Semaine / Mois / Année /
    Personnalisée) appliqué côté backend (`?start=&end=` sur `/api/reports/export/`,
    période rappelée dans le sous-titre PDF ; dates invalides → 400).

- ✅ **Phase 28** — **Paramètres : gestion complète des utilisateurs** :
  - Onglet **Utilisateurs** (administrateurs) : stats (actifs/bloqués/admins), recherche +
    filtres rôle/statut, **création** (rôle, filiale, mot de passe initial), **modification**
    (dont changement de rôle), **blocage/déblocage** (connexion refusée, historique conservé),
    **mots de passe** (définir un mot de passe précis ou **générer un temporaire** affiché
    une seule fois avec bouton copier), **suppression** (= désactivation ; suppression
    définitive réservée au super administrateur via `?hard=true`).
  - **Garde-fous serveur** (testés) : pas d'auto-blocage/suppression, rôles à périmètre
    entreprise réservés au siège, comptes super admin gérés par super admin uniquement,
    toutes les actions auditées.
  - Onglet **Mon compte** : profil + **changer mon mot de passe** en self-service
    (`POST /api/auth/change-password/`, mot de passe actuel vérifié) + thème + Web Push.

- ✅ **Phase 29** — **Notifications email automatiques globales + centre de notifications** :
  - **Couverture complète des événements** : réservations (création, soumission,
    validation/rejet par niveau, affectations véhicule/chauffeur, modification, annulation,
    départ, retard, retour, clôture), **carburant** (plein déclaré, écart estimé/réel > 20 %,
    prix CI mis à jour), **maintenance** (panne déclarée, maintenance planifiée/terminée,
    véhicule immobilisé/remis en service, coût → finance), conformité (déjà en place).
  - **Parties prenantes notifiées** : demandeur, responsable hiérarchique, gestionnaires de
    flotte, admin filiale/entreprise, chauffeur, finance — chaque email de réservation
    contient **n°, demandeur, filiale, dates, départ/destination, statut, prochaine action
    attendue et lien direct** (`apps/notifications/events.py`).
  - **Traçabilité** : `EmailLog` (statut envoyé/échec/désactivé/préférence) avec **relance
    manuelle** (`POST /api/notification-emails/{id}/resend/`) ; **modèles d'emails
    personnalisables** (`EmailTemplate` dans l'admin, placeholders {title}/{message}/{link}/
    {recipient}) ; **préférences par type et par canal** (interne/email/push) respectées
    par `notify()` (`/api/notification-preferences/`).
  - **Centre de notifications** `/notifications` (tous rôles) : flux filtrable
    (sévérité, non lues) + marquer tout lu ; **historique des emails** avec statut et bouton
    Relancer (admins) ; **préférences** éditables par l'utilisateur.
  - **Révision configurable PAR VÉHICULE** : champ `revision_interval_km` (Hilux 10 000,
    Duster 5 000, camion 15 000…), prochaine révision = dernière + intervalle du véhicule,
    **seuils d'alerte en % de l'intervalle** (20/10/5/0/dépassement, configurables via
    `REVISION_ALERT_PCTS`), affiché et éditable dans la fiche véhicule.

- ✅ **Phase 30** — **Production-ready + déploiement Docker / Dokploy** :
  - `config/settings/production.py` : `DEBUG=False`, `SECRET_KEY`/`ALLOWED_HOSTS`
    obligatoires, **WhiteNoise** (statiques compressés + manifest), durcissement HTTP
    (cookies sécurisés, HSTS, `SECURE_PROXY_SSL_HEADER`, X-Frame DENY), connexions
    PostgreSQL persistantes, **SMTP** conditionnel, logs stdout. `manage.py check
    --deploy` propre.
  - **Endpoint `/healthz/`** (sans auth) pour les sondes Docker ; médias servis en
    production (volume dédié).
  - **Images Docker** : backend (`python:3.14-slim`, Daphne ASGI, utilisateur non-root,
    entrypoint migrations + `collectstatic` + rôles + superuser optionnel) ; frontend
    (Next.js **standalone**, `NEXT_PUBLIC_API_BASE` figé au build). `celery`/`redis`
    ajoutés aux dépendances.
  - **`docker-compose.yml` Dokploy** : `db` (PostgreSQL 16), `redis`, `backend`,
    `worker` (Celery), `beat` (Celery beat), `frontend` — healthchecks, dépendances
    `service_healthy`, réseau `dokploy-network` (Traefik) + réseau interne, volumes
    persistants, ancre d'env partagée. `.env.production.example` documente toutes les
    variables.
  - `.gitignore` : tous les `.md` ignorés (+ `.next/`, `node_modules/`, secrets).
  - Vérifié : image backend construite, `docker compose config` valide, build frontend
    standalone (server.js), 37/37 tests, revue adversariale en 5 dimensions (Docker,
    Compose/Dokploy, Django, frontend/WS, contrat d'env) → correctifs appliqués.

### Prochaines phases (infrastructure optionnelle)

10. Celery (alertes périodiques, rappels), Web Push (notifications), géofencing temps réel,
    offline IndexedDB + Background Sync, fan-out WebSocket par groupes Redis (multi-worker).
5. GPS : PostGIS, tracking live, géofencing, replay d'itinéraire, mode offline
6. IA : suggestions, prévisions maintenance, détection d'anomalies
7. Exports (PDF/Excel/CSV), Docker, monitoring, sauvegardes
