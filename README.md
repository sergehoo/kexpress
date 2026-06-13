[//]: # (# Kaydan Express — Backend)

[//]: # ()
[//]: # (Plateforme de gestion de flotte multi-filiales &#40;Django + DRF&#41;. **Phase 1 : fondation backend.**)

[//]: # ()
[//]: # (## Stack &#40;Phase 1&#41;)

[//]: # ()
[//]: # (- Python 3.14 · Django 6 · Django REST Framework)

[//]: # (- PostgreSQL 16 &#40;local&#41; · SimpleJWT · drf-spectacular &#40;OpenAPI&#41;)

[//]: # (- *Différé aux phases suivantes :* Next.js PWA, Channels/WebSocket, Celery/Redis,)

[//]: # (  PostGIS, Docker, IA, exports PDF/Excel.)

[//]: # ()
[//]: # (## Architecture)

[//]: # ()
[//]: # (- **Multi-filiales** : `apps/core` fournit `TimeStampedModel` &#40;PK UUID + horodatage&#41;)

[//]: # (  et `TenantScopedModel` &#40;FK `subsidiary` + `TenantManager.for_user&#40;&#41;` qui filtre par périmètre&#41;.)

[//]: # (- **Isolation** : `TenantScopedViewSetMixin` restreint chaque queryset au périmètre de)

[//]: # (  l'utilisateur. Les rôles à périmètre entreprise &#40;super admin, admin entreprise, auditeur&#41;)

[//]: # (  voient toutes les filiales ; les autres uniquement la leur ; un employé demandeur ne voit)

[//]: # (  que ses propres réservations.)

[//]: # (- **Rôles** : champ `role` sur `accounts.User` + un groupe Django par rôle)

[//]: # (  &#40;commande `setup_roles`&#41;.)

[//]: # ()
[//]: # (### Apps)

[//]: # ()
[//]: # (| App | Rôle |)

[//]: # (|-----|------|)

[//]: # (| `core` | bases abstraites, enums, permissions, pagination, mixins |)

[//]: # (| `accounts` | User custom &#40;login email&#41;, rôles, auth JWT |)

[//]: # (| `organizations` | Company → Subsidiary → Department |)

[//]: # (| `vehicles` | véhicules, documents, historique de statut |)

[//]: # (| `drivers` | chauffeurs, disponibilités, évaluations, incidents |)

[//]: # (| `reservations` | réservations + workflow de validation configurable |)

[//]: # (| `trips` | courses &#40;exécution réelle&#41;, remise/retour, incidents, photos |)

[//]: # (| `maintenance` | types, échéances, interventions |)

[//]: # (| `expenses` | carburant, dépenses, budget flotte |)

[//]: # (| `tracking` | GPS, sessions, itinéraires, géofencing, sync offline |)

[//]: # (| `notifications` | notifications + préférences de canal |)

[//]: # (| `audit` | journal d'audit générique |)

[//]: # ()
[//]: # (> Les modèles GPS utilisent des coordonnées `Decimal` en Phase 1 ; migration vers)

[//]: # (> PostGIS `PointField` prévue à la phase temps réel.)

[//]: # ()
[//]: # (## Démarrage)

[//]: # ()
[//]: # (PostgreSQL doit tourner. En dev local, l'instance Homebrew `postgresql@16` est lancée)

[//]: # (sur le **port 5433** &#40;auth `trust`&#41; :)

[//]: # ()
[//]: # (```bash)

[//]: # (/opt/homebrew/opt/postgresql@16/bin/pg_ctl -D /opt/homebrew/var/postgresql@16 -o "-p 5433" start)

[//]: # (```)

[//]: # ()
[//]: # (Puis :)

[//]: # ()
[//]: # (```bash)

[//]: # (cp .env.example .env                      # adapter si besoin &#40;DATABASE_URL&#41;)

[//]: # (.venv/bin/pip install -r requirements/local.txt)

[//]: # (.venv/bin/python manage.py migrate)

[//]: # (.venv/bin/python manage.py setup_roles    # crée les groupes de rôles)

[//]: # (.venv/bin/python manage.py seed_demo      # données de démonstration)

[//]: # (.venv/bin/python manage.py runserver)

[//]: # (```)

[//]: # ()
[//]: # (## Comptes de démonstration)

[//]: # ()
[//]: # (Tous avec le mot de passe **`demo1234`** :)

[//]: # ()
[//]: # (| Email | Rôle | Périmètre |)

[//]: # (|-------|------|-----------|)

[//]: # (| `super@kaydan.test` | Super admin | global &#40;accès `/admin/`&#41; |)

[//]: # (| `admin@kaydan.test` | Admin entreprise | toutes filiales |)

[//]: # (| `admin.abj@kaydan.test` | Admin filiale | Abidjan |)

[//]: # (| `flotte.abj@kaydan.test` | Gestionnaire flotte | Abidjan |)

[//]: # (| `resp.abj@kaydan.test` | Responsable service | Abidjan |)

[//]: # (| `employe.abj@kaydan.test` | Employé demandeur | Abidjan &#40;ses demandes&#41; |)

[//]: # (| `chauffeur.abj@kaydan.test` | Chauffeur | Abidjan |)

[//]: # (| `finance@kaydan.test` | Finance | global |)

[//]: # (| `audit@kaydan.test` | Auditeur | global &#40;lecture&#41; |)

[//]: # ()
[//]: # (## Endpoints clés)

[//]: # ()
[//]: # (- `POST /api/auth/token/` · `POST /api/auth/refresh/` · `POST /api/auth/verify/`)

[//]: # (- `GET  /api/auth/me/` — profil + rôle + périmètre)

[//]: # (- `GET/POST /api/vehicles/` · `GET/POST /api/reservations/` · `GET /api/trips/` &#40;scoping automatique&#41;)

[//]: # (- `GET  /api/schema/` &#40;OpenAPI&#41; · `GET /api/docs/` &#40;Swagger UI&#41;)

[//]: # (- `GET  /admin/` &#40;administration Django, tous les modèles enregistrés&#41;)

[//]: # ()
[//]: # (### Workflow réservation → course &#40;Phase 2&#41;)

[//]: # ()
[//]: # (Actions `POST` sur `/api/reservations/{id}/` :)

[//]: # (`submit` · `approve` · `reject` · `cancel` · `assign-vehicle` · `assign-driver`.)

[//]: # ()
[//]: # (Actions `POST` sur `/api/trips/{id}/` : `start` · `end` · `close`.)

[//]: # ()
[//]: # (Machine à états &#40;statuts de réservation&#41; :)

[//]: # ()
[//]: # (```)

[//]: # (DRAFT → &#40;submit&#41; → PENDING_MANAGER → &#40;approve&#41; → PENDING_FLEET → &#40;approve&#41; → APPROVED)

[//]: # (      → &#40;assign-vehicle&#41; → VEHICLE_ASSIGNED → &#40;assign-driver&#41; → DRIVER_ASSIGNED)

[//]: # (      → &#40;trip.start&#41; → IN_PROGRESS → &#40;trip.end&#41; → COMPLETED → &#40;trip.close&#41; → CLOSED)

[//]: # (      reject → REJECTED   |   cancel → CANCELLED)

[//]: # (```)

[//]: # ()
[//]: # (Contrôles automatiques à l'affectation &#40;§4&#41; : cohérence de durée, capacité du véhicule,)

[//]: # (disponibilité véhicule/chauffeur, **conflits horaires** &#40;chevauchement avec une autre)

[//]: # (réservation active&#41;, isolation par filiale. Chaque transition crée des notifications)

[//]: # (internes et une entrée de **journal d'audit**, et met à jour le statut du véhicule)

[//]: # (&#40;disponible → réservé → en course → disponible&#41;.)

[//]: # ()
[//]: # (Le workflow de validation est **configurable** via `ApprovalWorkflow`/`ApprovalStep`)

[//]: # (&#40;par filiale ou global&#41; ; à défaut, l'enchaînement responsable → flotte s'applique.)

[//]: # ()
[//]: # (## Tests)

[//]: # ()
[//]: # (```bash)

[//]: # (.venv/bin/python -m pytest tests/)

[//]: # (```)

[//]: # ()
[//]: # (Couvre l'isolation multi-filiales &#40;manager + API&#41; et l'endpoint `/me`.)

[//]: # ()
[//]: # (## Phases)

[//]: # ()
[//]: # (- ✅ **Phase 1** — fondation backend : modèles, multi-filiales, rôles, auth JWT, admin, OpenAPI.)

[//]: # (- ✅ **Phase 2** — logique métier : workflow de validation configurable, affectation)

[//]: # (  véhicule/chauffeur avec contrôles &#40;conflits, capacité, disponibilité&#41;, cycle de vie des)

[//]: # (  courses &#40;départ/retour/clôture&#41;, notifications internes, journal d'audit.)

[//]: # (- ✅ **Phase 3** — frontend Next.js PWA &#40;`frontend/`&#41; : auth JWT, dashboard, véhicules,)

[//]: # (  réservations avec actions de workflow, courses, responsive mobile-first, installable + offline.)

[//]: # (- ✅ **Phase 4** — UI premium : thème **orange / bleu nuit** + **mode clair/sombre**, shell)

[//]: # (  premium &#40;sidebar groupée à icônes, topbar avec recherche globale, sélecteur de filiale,)

[//]: # (  notifications, profil&#41;, **dashboard intelligent** &#40;8 KPIs, graphiques Recharts, widgets IA :)

[//]: # (  résumé du jour, alertes, score santé flotte&#41;, assistant **K-BOT** flottant ancré sur les)

[//]: # (  données, et toutes les pages de navigation &#40;24 routes&#41;. Nouveaux endpoints backend :)

[//]: # (  `/api/dashboard/stats/`, `/api/kbot/ask/`, `/api/subsidiaries/`, `/api/notifications/`.)

[//]: # ()
[//]: # (- ✅ **Phase 5** — Centre de contrôle & données réelles : cartes **Leaflet** &#40;Fleet Control)

[//]: # (  Center avec liste + filtres + détail, et Carte temps réel&#41;, endpoint positions GPS scopé)

[//]: # (  &#40;`/api/tracking/positions/`&#41; + positions de démo, pages **réelles** Chauffeurs / Maintenance /)

[//]: # (  Carburant &#40;API `/api/drivers/`, `/api/maintenance/`, `/api/fuel/`, `/api/expenses/`&#41;, et)

[//]: # (  **K-BOT sur LLM réel** activable par `ANTHROPIC_API_KEY` &#40;RAG ancré sur les données)

[//]: # (  autorisées ; repli heuristique automatique sans clé&#41;.)

[//]: # ()
[//]: # (- ✅ **Phase 6** — Temps réel WebSocket : stack **Django Channels + Redis** &#40;ASGI/daphne&#41;,)

[//]: # (  consumer `FleetConsumer` authentifié JWT qui **pousse les positions** &#40;toutes les 4 s, scopé)

[//]: # (  filiale&#41; → remplace le polling REST &#40;repli REST conservé&#41;. Icônes véhicule + nom du chauffeur)

[//]: # (  + focus/zoom au clic.)

[//]: # (  **Itinéraire façon Google Maps** : routage routier **OSRM** &#40;suivi des routes réelles, cache)

[//]: # (  `TripRoute.geometry`&#41;, le véhicule **progresse le long du tracé**, polyline *prévu* &#40;route)

[//]: # (  bleue + casing&#41; vs *réel* &#40;trace orange parcourue&#41;, **marqueur de destination** 🏁, carte)

[//]: # (  d'info **distance / durée / progression / vitesse** &#40;endpoint `/api/tracking/trips/<id>/route/`&#41;.)

[//]: # (  Configurable via `OSRM_URL`.)

[//]: # ()
[//]: # (- ✅ **Phase 7** — Pages de données réelles : **Filiales**, **Employés**, **Dépenses**,)

[//]: # (  **Alertes** &#40;agrégées : expirations assurance/visite/permis, maintenance due, retards&#41;,)

[//]: # (  **Journal d'audit** &#40;réservé périmètre entreprise/auditeur&#41;. Nouveaux endpoints :)

[//]: # (  `/api/employees/`, `/api/audit/`, `/api/alerts/` &#40;+ `/api/expenses/`&#41;. Seed enrichi)

[//]: # (  &#40;documents véhicule, échéances, dépenses, permis&#41;.)

[//]: # ()
[//]: # (- ✅ **Phase 8** — Planning **FullCalendar** &#40;véhicules & chauffeurs, événements depuis les)

[//]: # (  réservations, couleurs par statut, vues semaine/mois/liste, thème clair/sombre&#41;, page)

[//]: # (  **Incidents** &#40;endpoint `/api/incidents/` agrégeant incidents course + chauffeur&#41;, page)

[//]: # (  **Paramètres** &#40;profil, thème, sécurité/périmètre, infos app&#41;.)

[//]: # ()
[//]: # (- ✅ **Phase 9** — **Rapports exportables** : endpoint `/api/reports/export/?type=&fmt=`)

[//]: # (  &#40;parc, dépenses, maintenance, courses&#41; en **CSV** &#40;UTF-8 BOM&#41;, **Excel** &#40;openpyxl, en-têtes)

[//]: # (  stylés&#41; et **PDF** &#40;reportlab, tableau paysage&#41;. Scoping par périmètre + filiale sélectionnée,)

[//]: # (  export **journalisé en audit**. Page Rapports avec téléchargements authentifiés &#40;blob&#41;.)

[//]: # (  **Toutes les pages de l'application sont désormais réelles.**)

[//]: # ()
[//]: # (- ✅ **Phase 10** — **CRUD complet** sur les entités : infrastructure réutilisable côté front)

[//]: # (  &#40;`useCrud`, `EntityForm` piloté par field-spec, `RowActions` créer/éditer/supprimer + confirmation&#41;,)

[//]: # (  endpoints backend **écrivables** et scopés &#40;Véhicules, Chauffeurs, Maintenance, Carburant,)

[//]: # (  Dépenses, Filiales, Employés&#41;. Permissions : écriture filiales/employés réservée aux)

[//]: # (  administrateurs ; remplissage automatique de la filiale pour les rôles mono-filiale ;)

[//]: # (  suppression d'employé = désactivation &#40;préserve l'historique&#41;. Référentiel `maintenance-types`.)

[//]: # ()
[//]: # (- ✅ **Phase 11** — **Vue `/map` type Uber** : carte plein écran + panneau de réservation)

[//]: # (  &#40;latéral desktop / bottom-sheet mobile&#41;. Recherche de lieux **autocomplete** &#40;Nominatim +)

[//]: # (  filiales&#41;, **« Utiliser ma position »** &#40;géoloc&#41;, **clic carte** pour la destination,)

[//]: # (  **estimation auto** &#40;OSRM : distance / durée / **coût** + véhicules disponibles&#41;, **véhicules)

[//]: # (  proches** triés par distance/ETA, marqueurs départ/destination + itinéraire tracé, véhicules)

[//]: # (  live &#40;WebSocket&#41;, **réservation directe** &#40;Réserver maintenant / Planifier&#41;. Endpoints :)

[//]: # (  `/api/places/search/`, `/api/routes/estimate/`, `/api/map/nearby-vehicles/`,)

[//]: # (  `/api/reservations/from-map/`. MapView étendu &#40;clic, marqueur origine, recentrage géoloc&#41;.)

[//]: # ()
[//]: # (- ✅ **Phase 12** — **Suivi temps réel par course** sur `/map` : WebSocket)

[//]: # (  `ws/trips/<id>/tracking/` &#40;consumer authentifié JWT&#41; qui pousse la position du véhicule)

[//]: # (  affecté, l'état de la course, l'ETA et l'itinéraire &#40;prévu + trace réelle&#41; toutes les 4 s.)

[//]: # (  Endpoint `/api/trips/active/`. La page `/map` **bascule automatiquement en mode suivi** quand)

[//]: # (  l'utilisateur a une course en cours : panneau ETA + barre de progression, véhicule animé qui)

[//]: # (  suit la route, chauffeur, **suivi d'étapes** &#40;chauffeur affecté → en route → en cours →)

[//]: # (  arrivée&#41;, recentrage automatique. Le mode flotte est désactivé en suivi &#40;pas de double avance&#41;.)

[//]: # ()
[//]: # (- ✅ **Phase 13** — K-BOT intégré à `/map` : **suggestion ancrée** dans)

[//]: # (  `/api/map/nearby-vehicles/` &#40;meilleur véhicule + distance + ETA + niveau de disponibilité,)

[//]: # (  messages adaptés si aucun véhicule / pas de GPS&#41; affichée en carte « Suggestion K-BOT » dans)

[//]: # (  le panneau de réservation ; **destinations récentes** &#40;localStorage, 5 dernières&#41; proposées)

[//]: # (  au focus des champs départ/destination.)

[//]: # ()
[//]: # (- ✅ **Phase 14** — Identité visuelle & infrastructure :)

[//]: # (  - **Logo Kaydan Express** intégré &#40;sidebar, login, favicon, icônes PWA régénérées depuis l'emblème&#41;.)

[//]: # (  - **Celery + beat** &#40;Redis&#41; : tâches périodiques `check_expirations` &#40;documents/permis/maintenances,)

[//]: # (    2×/jour&#41; et `check_late_trips` &#40;5 min&#41;, anti-spam 24 h. Lancer : `celery -A config worker -B`.)

[//]: # (  - **Web Push réel &#40;VAPID&#41;** : clés dans `.env`, modèle `PushSubscription`, endpoints)

[//]: # (    `/api/push/vapid-key/` et `/api/push/subscribe/`, envoi best-effort dans `notify&#40;&#41;`)

[//]: # (    &#40;purge des abonnements expirés&#41;, bouton « Activer » dans Paramètres &#40;SW déjà prêt&#41;.)

[//]: # (  - **Géofencing temps réel** : `point_in_polygon` au tick GPS → `GeofenceAlert` sur transition)

[//]: # (    &#40;entrée/sortie&#41;, notifications critiques aux gestionnaires, zones dessinées sur la carte)

[//]: # (    du Centre de contrôle &#40;`/api/tracking/zones/`&#41;, alertes intégrées à `/api/alerts/`.)

[//]: # (  - **Offline IndexedDB** : outbox `kx-offline` — réservation créée hors ligne mise en file,)

[//]: # (    **synchronisation automatique** au retour réseau &#40;composant `OfflineSync` + indicateur&#41;.)

[//]: # ()
[//]: # (- ✅ **Phase 15** — Fin de course depuis la carte : boutons **« Terminer la course »**)

[//]: # (  &#40;km de retour optionnel — estimation automatique depuis la progression de l'itinéraire&#41;)

[//]: # (  et **« Clôturer la course »** directement dans le panneau de suivi de `/map`.)

[//]: # (  Une course « Retour effectué » reste active sur la carte jusqu'à clôture)

[//]: # (  &#40;`/api/trips/active/` inclut `returned`&#41;, puis la carte revient au mode réservation.)

[//]: # ()
[//]: # (- ✅ **Phase 16** — Page Réservations refondue : **statistiques cliquables** par phase)

[//]: # (  &#40;Brouillons / À valider / À affecter / En cours / Terminées&#41;, **recherche** multi-champs,)

[//]: # (  bascule **Liste / Kanban** &#40;colonnes par phase avec actions de workflow dans les cartes&#41;,)

[//]: # (  cartes enrichies &#40;accent coloré par statut, badge priorité, chips véhicule/chauffeur)

[//]: # (  affectés, **mini-timeline des validations** responsable/flotte&#41;. Serializer enrichi)

[//]: # (  &#40;`vehicle_registration`, `driver_name`&#41;.)

[//]: # ()
[//]: # (- ✅ **Phase 17** — Centre de contrôle « poste d'exploitation » :)

[//]: # (  - Onglets **Véhicules / Demandes** dans le panneau latéral : les demandes à traiter)

[//]: # (    &#40;validation + affectation&#41; sont gérées **sans quitter la carte** — Valider / Refuser /)

[//]: # (    Affecter véhicule / Affecter chauffeur / « Prête au départ » &#40;modales partagées)

[//]: # (    `components/reservation-modals.tsx`, réutilisées par la page Réservations&#41;.)

[//]: # (  - **Statistiques cliquables** &#40;En course / Disponibles / En retard / Maintenance&#41; qui)

[//]: # (    filtrent la liste et la carte.)

[//]: # (  - Cartographie : **sélecteur de fond de carte** Plan / Sombre / **Satellite** &#40;CARTO + Esri&#41;,)

[//]: # (    bouton **Suivre** &#40;la carte reste centrée sur le véhicule sélectionné à chaque tick GPS&#41;.)

[//]: # ()
[//]: # (- ✅ **Phase 18** — Flotte mutualisée & filiale déduite :)

[//]: # (  - **Aucune restriction de filiale** sur l'exploitation : véhicules et chauffeurs sont)

[//]: # (    visibles et affectables par toutes les filiales &#40;`FleetWideManager` sur Vehicle/Driver,)

[//]: # (    contrôles de filiale supprimés du workflow d'affectation&#41;. La filiale reste un simple)

[//]: # (    rattachement administratif.)

[//]: # (  - **`/map`** : plus de champ filiale — déduite du demandeur &#40;repli : 1re filiale active)

[//]: # (    pour les comptes entreprise&#41;.)

[//]: # (  - **`/reservations`** : le sélecteur de filiale est remplacé par une **autocomplétion)

[//]: # (    « Employé demandeur »** &#40;recherche serveur&#41; ; la filiale de la demande est déduite de)

[//]: # (    l'employé sélectionné.)

[//]: # ()
[//]: # (- ✅ **Phase 19** — Adresses **OpenStreetMap/Nominatim partout** : composant partagé)

[//]: # (  `PlaceSearch` &#40;suggestions pendant la saisie, **priorité Côte d'Ivoire** via)

[//]: # (  `countrycodes=ci` + complément mondial, filiales internes, destinations récentes,)

[//]: # (  texte libre accepté&#41;. Branché sur `/map` &#40;départ + destination&#41; et sur le champ)

[//]: # (  **Destination** du formulaire de réservation. Pays prioritaire configurable)

[//]: # (  &#40;`PLACES_PRIORITY_COUNTRIES`&#41;.)

[//]: # ()
[//]: # (- ✅ **Phase 20** — Géocodage inverse : endpoint `/api/places/reverse/` &#40;Nominatim&#41;.)

[//]: # (  Sur `/map`, « **Utiliser ma position actuelle** » remplit le champ Point de départ avec)

[//]: # (  **l'adresse réelle** de l'utilisateur &#40;état « Localisation en cours… », repli libellé)

[//]: # (  générique hors ligne&#41; ; le **clic sur la carte** géocode aussi l'adresse de destination.)

[//]: # (  `PlaceSearch` accepte une valeur poussée par le parent &#40;`externalValue`&#41;.)

[//]: # ()
[//]: # (- ✅ **Phase 21** — **Fuel Intelligence** &#40;le coût disparaît de l'expérience employé&#41; :)

[//]: # (  - L'estimation de trajet affiche **Distance / Temps / Consommation &#40;L&#41;** + niveau d'impact)

[//]: # (    énergétique — **plus aucun coût** pour l'employé demandeur.)

[//]: # (  - **Moteur apprenant** &#40;`apps/fuelintel`&#41; : profils de consommation L/100 km recalculés)

[//]: # (    périodiquement &#40;Celery, 6 h&#41; depuis les courses réelles, à 5 niveaux &#40;véhicule, chauffeur,)

[//]: # (    type, filiale, flotte&#41; avec lissage bayésien ; ajustement contextuel &#40;urbain/route,)

[//]: # (    heures de pointe&#41;. Plus la plateforme est utilisée, plus l'estimation est fiable.)

[//]: # (  - **Prix carburant CI** &#40;Super / Gasoil&#41; : modèle `FuelPrice` avec historique des variations,)

[//]: # (    mise à jour quotidienne configurable &#40;source JSON distante `FUEL_PRICE_SOURCE_URL` ou)

[//]: # (    valeurs réglementées en repli&#41;.)

[//]: # (  - **Visibilité par rôle** : gestionnaires/admins/finance voient coût, estimé vs réel, écart,)

[//]: # (    score d'efficacité &#40;`fuel_intel` sur les courses&#41; ; employés non &#40;`403` sur fuel-intel&#41;.)

[//]: # (  - **Fleet Fuel Intelligence** : onglet **Carburant** au Centre de contrôle &#40;conso jour/mois,)

[//]: # (    coûts, prévision mensuelle, écart prévu/réel, top consommateurs, chauffeurs économes,)

[//]: # (    alertes de surconsommation, prix CI + historique&#41; via `/api/fuel-intel/`.)

[//]: # (  - **K-BOT énergie** : chauffeurs les plus économes, filiales sobres, véhicules en)

[//]: # (    surconsommation / à remplacer &#40;ancré sur les profils appris&#41;.)

[//]: # ()
[//]: # (- ✅ **Phase 22** — **Vues détail commande & course** :)

[//]: # (  - `/reservations/[id]` : trajet demandé complet &#40;départ/destination/horaires/passagers/motif&#41;,)

[//]: # (    fiche demandeur &#40;nom, email, filiale&#41;, affectations véhicule/chauffeur, **circuit de)

[//]: # (    validation** détaillé &#40;validateur, décision, commentaire, horodatage&#41;, actions du workflow)

[//]: # (    contextuelles, lien direct vers la course créée &#40;`trip_id`&#41;.)

[//]: # (  - `/trips/[id]` : **carte de l'itinéraire** &#40;prévu/réel, progression, vitesse&#41;, exécution)

[//]: # (    réelle &#40;départ/retour, km, distance&#41;, participants, **bloc Carburant & énergie**)

[//]: # (    &#40;estimé/réel pour tous ; écart, score et coût réservés aux gestionnaires&#41;, incidents de)

[//]: # (    course, actions démarrer/terminer/clôturer, lien retour vers la commande.)

[//]: # (  - Navigation : titres cliquables + bouton « Détails » sur les listes Réservations et)

[//]: # (    Courses, et dans l'onglet Demandes du Centre de contrôle.)

[//]: # ()
[//]: # (- ✅ **Phase 23** — **Tracking 100 % données réelles** &#40;fin de la simulation&#41; :)

[//]: # (  - Le simulateur serveur &#40;avancement fictif le long de la route + vitesses aléatoires)

[//]: # (    28-52 km/h&#41; est **supprimé** : la lecture des positions est strictement passive.)

[//]: # (  - **Ingestion GPS réelle** : `POST /api/tracking/trips/{id}/position/` — l'appareil d'un)

[//]: # (    participant &#40;demandeur, chauffeur, gestionnaire&#41; pousse sa position pendant la course ;)

[//]: # (    vitesse fournie par le capteur ou dérivée de l'intervalle réel entre deux points.)

[//]: # (  - Côté PWA, `useGpsTracker` &#40;watchPosition&#41; émet automatiquement pendant une course en)

[//]: # (    cours sur `/map` et sur le détail de course, avec indicateur d'état GPS.)

[//]: # (  - **Calculs honnêtes** : distance parcourue = somme des segments GPS réels ; progression)

[//]: # (    = parcouru/itinéraire ; vitesse affichée uniquement si la position date de < 3 min)

[//]: # (    &#40;sinon « — »&#41; ; **sauts GPS filtrés** &#40;vitesse implicite > 160 km/h exclue du cumul&#41;.)

[//]: # (  - `end_trip` sans kilométrage saisi s'appuie sur la **distance GPS réelle** &#40;repli :)

[//]: # (    distance routière planifiée&#41; ; les sessions de tracking sont figées à la fin de course)

[//]: # (    &#40;distance totale + vitesse moyenne réelles&#41;. Géofencing déclenché à l'ingestion.)

[//]: # (  - 6 tests dédiés &#40;lecture passive, dérivation de vitesse, anti-spam, saut GPS,)

[//]: # (    vitesse périmée, participant requis&#41;.)

[//]: # ()
[//]: # (- ✅ **Phase 24** — **Dashboard décisionnel + maintenance avancée + conformité flotte** :)

[//]: # (  - **Dashboard décisionnel** &#40;`/api/dashboard/stats/` v2&#41; : filtres **période**)

[//]: # (    &#40;semaine/mois/année/personnalisée&#41;, **statut de réservation**, filiale ; KPIs)

[//]: # (    réservations &#40;total, validées, rejetées, annulées, en attente, taux, **temps moyen de)

[//]: # (    traitement**&#41;, exploitation &#40;km, courses, **temps moyen d'utilisation**, retards,)

[//]: # (    incidents&#41;, carburant &#40;estimé vs réel, écart, coûts&#41; ; **Coût total flotte =)

[//]: # (    dépenses générales + maintenance + carburant des courses** avec détail en 10 lignes ;)

[//]: # (    courbes &#40;réservations, validées/rejetées/annulées, carburant L+coût, km&#41; ;)

[//]: # (    **évolution des coûts par filiale** ; tops véhicules/courses coûteux. Les stats)

[//]: # (    « nombre de véhicules par filiale » sont retirées.)

[//]: # (  - **Imputation des charges par filiale** : une dépense, un plein ou une maintenance)

[//]: # (    liés à une course sont automatiquement rattachés à la **filiale de la course**)

[//]: # (    &#40;`save&#40;&#41;` sur MaintenanceRecord/Expense/FuelLog&#41; — testé &#40;crevaison → filiale course&#41;.)

[//]: # (  - **Maintenance enrichie** : nature &#40;préventive/corrective/urgente/périodique&#41;,)

[//]: # (    **nomenclature de pannes configurable** &#40;`BreakdownType`, 14 entrées&#41;, course liée,)

[//]: # (    dates déclaration/immobilisation début-fin + **durée d'indisponibilité**, coûts)

[//]: # (    **main-d'œuvre + pièces** &#40;total auto&#41;, garage, justificatif/photo, responsable de)

[//]: # (    validation ; 15 types de maintenance seedés ; KPIs maintenance au dashboard)

[//]: # (    &#40;préventive vs corrective, top pannes, indisponibilité par véhicule, taux)

[//]: # (    d'immobilisation, annulations pour panne&#41;.)

[//]: # (  - **Conformité véhicules** : modèles `InsurancePolicy`, `TechnicalInspection`,)

[//]: # (    `VehicleRevision` &#40;révision tous les **10 000 km** : prochaine = dernière + 10 000&#41; ;)

[//]: # (    badge Conforme/Non conforme + raisons + échéances sur la page Véhicules ; un véhicule)

[//]: # (    **non conforme est bloqué à l'affectation** &#40;raison affichée + véhicule conforme)

[//]: # (    suggéré, option désactivée dans la modale&#41; ; **rappels Celery** par paliers)

[//]: # (    &#40;J-30/15/7/0 + après expiration ; 2 000/1 000/500/0 km + dépassement&#41; avec)

[//]: # (    anti-doublon par palier ; KPIs conformité &#40;taux, renouvellements, coûts annuels&#41;.)

[//]: # ()
[//]: # (- ✅ **Phase 25** — **Fiche véhicule + alertes conformité partout + email** :)

[//]: # (  - **Fiche véhicule** `/vehicles/[id]` : bandeau conformité &#40;statut, raisons, échéances)

[//]: # (    assurance/visite/révision&#41;, infos générales, **saisie + historique** des assurances,)

[//]: # (    visites techniques et révisions &#40;formulaires « Ajouter »&#41;, maintenances du véhicule.)

[//]: # (    Cycle vérifié : révision saisie au kilométrage courant → véhicule **redevient)

[//]: # (    conforme** instantanément &#40;prochain seuil +10 000 km&#41;.)

[//]: # (  - **Alertes conformité dans le flux global** `/api/alerts/` &#40;types insurance /)

[//]: # (    inspection / revision&#41; → visibles page Alertes, dashboard et centre de contrôle.)

[//]: # (  - **Canal email** sur `notify&#40;&#41;` &#40;meilleur effort&#41; : activable via)

[//]: # (    `NOTIFY_EMAIL_ENABLED` + `EMAIL_*`/`DEFAULT_FROM_EMAIL` en environnement)

[//]: # (    &#40;backend console en local, SMTP en production&#41;. SMS/WhatsApp restent optionnels.)

[//]: # ()
[//]: # (- ✅ **Phase 26** — **Vues organisées par rôle** &#40;`frontend/src/lib/rbac.ts`&#41; :)

[//]: # (  - Matrice rôle → pages &#40;miroir de `RoleChoices`&#41; : la sidebar n'affiche que les modules)

[//]: # (    du métier ; badge du rôle dans la sidebar ; **garde de route** &#40;une URL hors périmètre)

[//]: # (    redirige vers l'accueil du rôle&#41; ; **atterrissage par rôle** après connexion)

[//]: # (    &#40;employé/chauffeur → Carte temps réel, gestion → Tableau de bord&#41;.)

[//]: # (  - Périmètres : admins &#40;tout&#41; · gestionnaire flotte &#40;pilotage + exploitation + flotte +)

[//]: # (    finance + incidents/alertes&#41; · responsable de service &#40;dashboard, carte, réservations,)

[//]: # (    planning véhicules, courses, alertes&#41; · **employé** &#40;carte, réservations, courses&#41; ·)

[//]: # (    **chauffeur** &#40;carte, courses, incidents&#41; · **finance** &#40;dashboard, véhicules,)

[//]: # (    maintenance, carburant, dépenses, rapports, alertes&#41; · **auditeur** &#40;dashboard,)

[//]: # (    réservations, courses, véhicules, rapports, incidents, **journal d'audit**&#41;.)

[//]: # (  - La sécurité des données reste assurée côté API &#40;scoping/permissions DRF&#41; — le RBAC)

[//]: # (    frontend organise l'expérience.)

[//]: # ()
[//]: # (- ✅ **Phase 27** — **Statistiques de liste + vues détail + exports périodiques** :)

[//]: # (  - Composant `StatChips` réutilisable : rangée de KPIs en tête de **chaque page de liste**)

[//]: # (    &#40;plannings, courses, véhicules, chauffeurs, maintenance, dépenses&#41;.)

[//]: # (  - **Plannings véhicules & chauffeurs** : stats &#40;planifiées, en cours, taux de)

[//]: # (    planification/charge&#41; + **détail au clic sur un créneau** &#40;modale + lien fiche&#41;.)

[//]: # (  - **Courses** : courses affichées, en cours, terminées, km cumulés, carburant réel.)

[//]: # (  - **Véhicules** : disponibles, en course, maintenance, **taux de conformité**.)

[//]: # (  - **Chauffeurs** : disponibles, permis ≤ 30 j, note moyenne + **fiche chauffeur**)

[//]: # (    `/drivers/[id]` &#40;coordonnées, permis avec alerte d'expiration, stats d'activité)

[//]: # (    réelles — courses, km, carburant, L/100 km — et historique des courses&#41;.)

[//]: # (  - **Dépenses** : montant total/moyen, top catégorie, liées à un véhicule + **détail au)

[//]: # (    clic sur une ligne** + champ course liée &#40;imputation filiale auto&#41;.)

[//]: # (  - **Rapports** : **filtre périodique d'export** &#40;Tout / Semaine / Mois / Année /)

[//]: # (    Personnalisée&#41; appliqué côté backend &#40;`?start=&end=` sur `/api/reports/export/`,)

[//]: # (    période rappelée dans le sous-titre PDF ; dates invalides → 400&#41;.)

[//]: # ()
[//]: # (- ✅ **Phase 28** — **Paramètres : gestion complète des utilisateurs** :)

[//]: # (  - Onglet **Utilisateurs** &#40;administrateurs&#41; : stats &#40;actifs/bloqués/admins&#41;, recherche +)

[//]: # (    filtres rôle/statut, **création** &#40;rôle, filiale, mot de passe initial&#41;, **modification**)

[//]: # (    &#40;dont changement de rôle&#41;, **blocage/déblocage** &#40;connexion refusée, historique conservé&#41;,)

[//]: # (    **mots de passe** &#40;définir un mot de passe précis ou **générer un temporaire** affiché)

[//]: # (    une seule fois avec bouton copier&#41;, **suppression** &#40;= désactivation ; suppression)

[//]: # (    définitive réservée au super administrateur via `?hard=true`&#41;.)

[//]: # (  - **Garde-fous serveur** &#40;testés&#41; : pas d'auto-blocage/suppression, rôles à périmètre)

[//]: # (    entreprise réservés au siège, comptes super admin gérés par super admin uniquement,)

[//]: # (    toutes les actions auditées.)

[//]: # (  - Onglet **Mon compte** : profil + **changer mon mot de passe** en self-service)

[//]: # (    &#40;`POST /api/auth/change-password/`, mot de passe actuel vérifié&#41; + thème + Web Push.)

[//]: # ()
[//]: # (- ✅ **Phase 29** — **Notifications email automatiques globales + centre de notifications** :)

[//]: # (  - **Couverture complète des événements** : réservations &#40;création, soumission,)

[//]: # (    validation/rejet par niveau, affectations véhicule/chauffeur, modification, annulation,)

[//]: # (    départ, retard, retour, clôture&#41;, **carburant** &#40;plein déclaré, écart estimé/réel > 20 %,)

[//]: # (    prix CI mis à jour&#41;, **maintenance** &#40;panne déclarée, maintenance planifiée/terminée,)

[//]: # (    véhicule immobilisé/remis en service, coût → finance&#41;, conformité &#40;déjà en place&#41;.)

[//]: # (  - **Parties prenantes notifiées** : demandeur, responsable hiérarchique, gestionnaires de)

[//]: # (    flotte, admin filiale/entreprise, chauffeur, finance — chaque email de réservation)

[//]: # (    contient **n°, demandeur, filiale, dates, départ/destination, statut, prochaine action)

[//]: # (    attendue et lien direct** &#40;`apps/notifications/events.py`&#41;.)

[//]: # (  - **Traçabilité** : `EmailLog` &#40;statut envoyé/échec/désactivé/préférence&#41; avec **relance)

[//]: # (    manuelle** &#40;`POST /api/notification-emails/{id}/resend/`&#41; ; **modèles d'emails)

[//]: # (    personnalisables** &#40;`EmailTemplate` dans l'admin, placeholders {title}/{message}/{link}/)

[//]: # (    {recipient}&#41; ; **préférences par type et par canal** &#40;interne/email/push&#41; respectées)

[//]: # (    par `notify&#40;&#41;` &#40;`/api/notification-preferences/`&#41;.)

[//]: # (  - **Centre de notifications** `/notifications` &#40;tous rôles&#41; : flux filtrable)

[//]: # (    &#40;sévérité, non lues&#41; + marquer tout lu ; **historique des emails** avec statut et bouton)

[//]: # (    Relancer &#40;admins&#41; ; **préférences** éditables par l'utilisateur.)

[//]: # (  - **Révision configurable PAR VÉHICULE** : champ `revision_interval_km` &#40;Hilux 10 000,)

[//]: # (    Duster 5 000, camion 15 000…&#41;, prochaine révision = dernière + intervalle du véhicule,)

[//]: # (    **seuils d'alerte en % de l'intervalle** &#40;20/10/5/0/dépassement, configurables via)

[//]: # (    `REVISION_ALERT_PCTS`&#41;, affiché et éditable dans la fiche véhicule.)

[//]: # ()
[//]: # (- ✅ **Phase 30** — **Production-ready + déploiement Docker / Dokploy** :)

[//]: # (  - `config/settings/production.py` : `DEBUG=False`, `SECRET_KEY`/`ALLOWED_HOSTS`)

[//]: # (    obligatoires, **WhiteNoise** &#40;statiques compressés + manifest&#41;, durcissement HTTP)

[//]: # (    &#40;cookies sécurisés, HSTS, `SECURE_PROXY_SSL_HEADER`, X-Frame DENY&#41;, connexions)

[//]: # (    PostgreSQL persistantes, **SMTP** conditionnel, logs stdout. `manage.py check)

[//]: # (    --deploy` propre.)

[//]: # (  - **Endpoint `/healthz/`** &#40;sans auth&#41; pour les sondes Docker ; médias servis en)

[//]: # (    production &#40;volume dédié&#41;.)

[//]: # (  - **Images Docker** : backend &#40;`python:3.14-slim`, Daphne ASGI, utilisateur non-root,)

[//]: # (    entrypoint migrations + `collectstatic` + rôles + superuser optionnel&#41; ; frontend)

[//]: # (    &#40;Next.js **standalone**, `NEXT_PUBLIC_API_BASE` figé au build&#41;. `celery`/`redis`)

[//]: # (    ajoutés aux dépendances.)

[//]: # (  - **`docker-compose.yml` Dokploy** : `db` &#40;PostgreSQL 16&#41;, `redis`, `backend`,)

[//]: # (    `worker` &#40;Celery&#41;, `beat` &#40;Celery beat&#41;, `frontend` — healthchecks, dépendances)

[//]: # (    `service_healthy`, réseau `dokploy-network` &#40;Traefik&#41; + réseau interne, volumes)

[//]: # (    persistants, ancre d'env partagée. `.env.production.example` documente toutes les)

[//]: # (    variables.)

[//]: # (  - `.gitignore` : tous les `.md` ignorés &#40;+ `.next/`, `node_modules/`, secrets&#41;.)

[//]: # (  - Vérifié : image backend construite, `docker compose config` valide, build frontend)

[//]: # (    standalone &#40;server.js&#41;, 37/37 tests, revue adversariale en 5 dimensions &#40;Docker,)

[//]: # (    Compose/Dokploy, Django, frontend/WS, contrat d'env&#41; → correctifs appliqués.)

[//]: # ()
[//]: # (### Prochaines phases &#40;infrastructure optionnelle&#41;)

[//]: # ()
[//]: # (10. Celery &#40;alertes périodiques, rappels&#41;, Web Push &#40;notifications&#41;, géofencing temps réel,)

[//]: # (    offline IndexedDB + Background Sync, fan-out WebSocket par groupes Redis &#40;multi-worker&#41;.)

[//]: # (5. GPS : PostGIS, tracking live, géofencing, replay d'itinéraire, mode offline)

[//]: # (6. IA : suggestions, prévisions maintenance, détection d'anomalies)

[//]: # (7. Exports &#40;PDF/Excel/CSV&#41;, Docker, monitoring, sauvegardes)
