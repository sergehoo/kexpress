#!/usr/bin/env bash
#
# [OPTIONNEL] Pré-amorçage manuel des données OSRM (auto-hébergé, open source).
#
# Depuis la refonte du docker-compose, la stack s'auto-amorce : les services
# kexpress-osrm-fetch (téléchargement) → kexpress-osrm-prepare
# (extract/partition/customize) → kexpress-osrm (serveur) préparent le graphe
# tout seuls au 1er `docker compose up` et le mettent en cache dans le volume
# nommé `kexpress_osrm`. AUCUNE étape manuelle n'est donc requise.
#
# Ce script reste utile pour pré-générer le graphe hors-ligne ou rafraîchir la
# carte en local (il écrit dans ./osrm-data, pas dans le volume nommé prod).
#
# Usage :
#   bash docker/osrm-setup.sh
#   docker compose up -d kexpress-osrm
#
# Variables :
#   OSRM_REGION_URL  URL de l'extrait .osm.pbf (défaut : Côte d'Ivoire / Geofabrik)
#   OSRM_PROFILE     profil de routage (défaut : car)
set -euo pipefail

DATA_DIR="${OSRM_DATA_DIR:-./osrm-data}"
REGION_URL="${OSRM_REGION_URL:-https://download.geofabrik.de/africa/ivory-coast-latest.osm.pbf}"
PROFILE="${OSRM_PROFILE:-/opt/car.lua}"
PBF="cote-divoire-latest.osm.pbf"
BASENAME="cote-divoire-latest"
IMAGE="osrm/osrm-backend:latest"

mkdir -p "$DATA_DIR"
cd "$DATA_DIR"

echo "▶ Téléchargement de l'extrait OSM : $REGION_URL"
if [ ! -f "$PBF" ]; then
  curl -fSL "$REGION_URL" -o "$PBF"
else
  echo "  (déjà présent, on réutilise $PBF — supprimez-le pour rafraîchir)"
fi

# Pipeline MLD (Multi-Level Dijkstra), cohérent avec `osrm-routed --algorithm mld`.
echo "▶ osrm-extract"
docker run --rm -t -v "$PWD:/data" "$IMAGE" osrm-extract -p "$PROFILE" "/data/$PBF"
echo "▶ osrm-partition"
docker run --rm -t -v "$PWD:/data" "$IMAGE" osrm-partition "/data/$BASENAME.osrm"
echo "▶ osrm-customize"
docker run --rm -t -v "$PWD:/data" "$IMAGE" osrm-customize "/data/$BASENAME.osrm"

echo "✅ Données prêtes dans $DATA_DIR ($BASENAME.osrm)."
echo "   Lancez : docker compose up -d kexpress-osrm"
echo "   Puis pointez le backend : OSRM_URL=http://kexpress-osrm:5000"
