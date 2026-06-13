#!/usr/bin/env sh
# Sauvegarde PostgreSQL planifiée pour Kaydan Express.
# Boucle simple et transparente (pas de cron) : dump compressé à intervalle régulier
# + purge des sauvegardes au-delà de la rétention. Image attendue : postgres:16-alpine
# (fournit pg_dump à la même version majeure que le serveur).
set -eu

POSTGRES_HOST="${POSTGRES_HOST:-kexpress-db}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_DB="${POSTGRES_DB:-kexpress}"
POSTGRES_USER="${POSTGRES_USER:-kexpress}"
BACKUP_DIR="${BACKUP_DIR:-/backups}"
BACKUP_INTERVAL_SECONDS="${BACKUP_INTERVAL_SECONDS:-86400}"  # 24 h par défaut
BACKUP_KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD requis}"
export PGPASSWORD="$POSTGRES_PASSWORD"

mkdir -p "$BACKUP_DIR"
echo "🗄  Sauvegarde Kaydan Express → ${BACKUP_DIR} (intervalle ${BACKUP_INTERVAL_SECONDS}s, rétention ${BACKUP_KEEP_DAYS}j)"

while true; do
    ts="$(date +%Y%m%d-%H%M%S)"
    out="${BACKUP_DIR}/kexpress-${ts}.sql.gz"
    echo "→ [$(date '+%Y-%m-%d %H:%M:%S')] Sauvegarde ${out}…"
    if pg_dump -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
        --no-owner --no-privileges --clean --if-exists | gzip > "${out}.tmp"; then
        mv "${out}.tmp" "$out"
        echo "✅ OK ($(du -h "$out" | cut -f1))"
    else
        echo "❌ Échec de la sauvegarde"
        rm -f "${out}.tmp"
    fi
    # Purge des sauvegardes plus anciennes que la rétention.
    find "$BACKUP_DIR" -name 'kexpress-*.sql.gz' -type f -mtime "+${BACKUP_KEEP_DAYS}" -delete 2>/dev/null || true
    sleep "$BACKUP_INTERVAL_SECONDS"
done
