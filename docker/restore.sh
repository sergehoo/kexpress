#!/usr/bin/env sh
# Restauration d'une sauvegarde PostgreSQL Kaydan Express.
# Usage (depuis le conteneur de sauvegarde) :
#   docker compose exec kexpress-backup sh /usr/local/bin/restore.sh            # dernière sauvegarde
#   docker compose exec kexpress-backup sh /usr/local/bin/restore.sh /backups/kexpress-AAAAMMJJ-HHMMSS.sql.gz
# ⚠ Écrase les données actuelles (les dumps sont créés avec --clean --if-exists).
set -eu

POSTGRES_HOST="${POSTGRES_HOST:-kexpress-db}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_DB="${POSTGRES_DB:-kexpress}"
POSTGRES_USER="${POSTGRES_USER:-kexpress}"
BACKUP_DIR="${BACKUP_DIR:-/backups}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD requis}"
export PGPASSWORD="$POSTGRES_PASSWORD"

FILE="${1:-$(ls -1t "${BACKUP_DIR}"/kexpress-*.sql.gz 2>/dev/null | head -1)}"
if [ -z "${FILE:-}" ] || [ ! -f "$FILE" ]; then
    echo "❌ Aucune sauvegarde trouvée (${FILE:-$BACKUP_DIR})."
    exit 1
fi

echo "♻️  Restauration de ${FILE} dans ${POSTGRES_DB}@${POSTGRES_HOST}…"
gunzip -c "$FILE" | psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DB"
echo "✅ Restauration terminée."
