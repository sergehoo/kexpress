#!/usr/bin/env sh
# Point d'entrée du conteneur backend : attend la base, applique les migrations
# (uniquement sur le service web via RUN_BOOTSTRAP), puis lance la commande passée.
set -e

echo "⏳ Attente de la base de données…"
python - <<'PY'
import os, sys, time
import psycopg

url = os.environ.get("DATABASE_URL", "")
for attempt in range(60):
    try:
        psycopg.connect(url, connect_timeout=3).close()
        print("✅ Base de données prête.")
        break
    except Exception as exc:  # noqa: BLE001
        print(f"… base indisponible (tentative {attempt + 1}/60) : {exc}")
        time.sleep(2)
else:
    print("❌ Base de données injoignable après 120 s.")
    sys.exit(1)
PY

# Bootstrap réservé au service web (évite les courses migrations entre conteneurs).
if [ "$RUN_BOOTSTRAP" = "true" ]; then
    echo "🛠  Migrations…"
    python manage.py migrate --noinput

    echo "👥 Rôles & permissions…"
    python manage.py setup_roles

    echo "🎨 Collecte des fichiers statiques…"
    python manage.py collectstatic --noinput

    # Superutilisateur initial (idempotent) si les variables sont fournies.
    if [ -n "$DJANGO_SUPERUSER_EMAIL" ] && [ -n "$DJANGO_SUPERUSER_PASSWORD" ]; then
        echo "👤 Vérification du superutilisateur…"
        python manage.py createsuperuser --noinput --email "$DJANGO_SUPERUSER_EMAIL" 2>/dev/null \
            && echo "✅ Superutilisateur créé." \
            || echo "ℹ️  Superutilisateur déjà présent."
    fi

    # Données de démonstration (désactivé par défaut en production).
    if [ "$SEED_DEMO" = "true" ]; then
        echo "🌱 Données de démonstration…"
        python manage.py seed_demo || true
    fi
fi

echo "🚀 Démarrage : $*"
exec "$@"
