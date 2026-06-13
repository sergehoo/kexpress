# Kaydan Express — image backend (Django + DRF + Channels/Daphne + Celery)
FROM python:3.14-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DJANGO_SETTINGS_MODULE=config.settings.production

WORKDIR /app

# curl pour les health checks ; le reste (psycopg, Pillow, reportlab, cryptography)
# s'installe via des wheels binaires, sans dépendances système supplémentaires.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Dépendances Python (couche cachée tant que les requirements ne changent pas).
COPY requirements/ requirements/
RUN pip install --upgrade pip && pip install -r requirements/production.txt

# Code applicatif.
COPY . .
RUN chmod +x docker/entrypoint.sh

# Utilisateur non-root.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/media /app/staticfiles \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

ENTRYPOINT ["./docker/entrypoint.sh"]
# Serveur ASGI (HTTP + WebSocket) : Daphne.
CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "config.asgi:application"]
