# Kaydan Express — image frontend (Next.js 15, sortie standalone)

# 1) Dépendances
FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

# 2) Build
FROM node:22-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# NEXT_PUBLIC_* est figé à la compilation : l'URL de l'API/WS doit être fournie ici.
ARG NEXT_PUBLIC_API_BASE
ENV NEXT_PUBLIC_API_BASE=${NEXT_PUBLIC_API_BASE}
# SSO Keycloak (vide = connexion locale ; renseigner pour activer le bouton SSO).
ARG NEXT_PUBLIC_OIDC_AUTHORITY
ARG NEXT_PUBLIC_OIDC_CLIENT_ID
ARG NEXT_PUBLIC_OIDC_MFA_ACR
ARG NEXT_PUBLIC_LOCAL_LOGIN
ENV NEXT_PUBLIC_OIDC_AUTHORITY=${NEXT_PUBLIC_OIDC_AUTHORITY}
ENV NEXT_PUBLIC_OIDC_CLIENT_ID=${NEXT_PUBLIC_OIDC_CLIENT_ID}
ENV NEXT_PUBLIC_OIDC_MFA_ACR=${NEXT_PUBLIC_OIDC_MFA_ACR}
ENV NEXT_PUBLIC_LOCAL_LOGIN=${NEXT_PUBLIC_LOCAL_LOGIN}
# Fonds de carte (cf. src/lib/mapConfig.ts). La clé CARTO est une clé NAVIGATEUR : elle figure
# dans l'URL des tuiles ; protégez-la en la restreignant aux domaines de l'application dans le
# tableau de bord CARTO. Vide = Plan/Sombre servis par les fournisseurs de secours.
ARG NEXT_PUBLIC_CARTO_API_KEY
ARG NEXT_PUBLIC_MAP_SATELLITE_URL
ARG NEXT_PUBLIC_MAP_SATELLITE_ATTRIBUTION
ARG NEXT_PUBLIC_MAP_SATELLITE_MAX_ZOOM
ARG NEXT_PUBLIC_MAP_VEHICLE_URL
ARG NEXT_PUBLIC_MAP_VEHICLE_ATTRIBUTION
ENV NEXT_PUBLIC_CARTO_API_KEY=${NEXT_PUBLIC_CARTO_API_KEY}
ENV NEXT_PUBLIC_MAP_VEHICLE_URL=${NEXT_PUBLIC_MAP_VEHICLE_URL}
ENV NEXT_PUBLIC_MAP_VEHICLE_ATTRIBUTION=${NEXT_PUBLIC_MAP_VEHICLE_ATTRIBUTION}
ENV NEXT_PUBLIC_MAP_SATELLITE_URL=${NEXT_PUBLIC_MAP_SATELLITE_URL}
ENV NEXT_PUBLIC_MAP_SATELLITE_ATTRIBUTION=${NEXT_PUBLIC_MAP_SATELLITE_ATTRIBUTION}
ENV NEXT_PUBLIC_MAP_SATELLITE_MAX_ZOOM=${NEXT_PUBLIC_MAP_SATELLITE_MAX_ZOOM}
ENV NEXT_TELEMETRY_DISABLED=1
# Avertissement de compilation (la valeur n'est jamais affichée).
RUN [ -n "$NEXT_PUBLIC_CARTO_API_KEY" ] || echo "AVERTISSEMENT : NEXT_PUBLIC_CARTO_API_KEY vide — fonds Plan/Sombre de secours."
RUN npm run build

# 3) Runtime (image minimale)
FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
RUN addgroup -g 1001 -S nodejs && adduser -S nextjs -u 1001
COPY --from=builder --chown=nextjs:nodejs /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
# Sonde de santé (utilisée par le healthcheck du compose).
COPY healthcheck.js ./healthcheck.js
USER nextjs
EXPOSE 3000
CMD ["node", "server.js"]
