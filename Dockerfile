# =============================================================================
# AI Frontends Hub — Hugging Face Space
#
# HF deploy: Create Space → Import from GitHub → Sexlovr/ai-hub-frontend
# SDK: Docker | Port: 7860 | Persistent storage mount: /data
#
# Frontends:
#   SillyTavern  — ghcr.io/sillytavern/sillytavern:latest
#   Marinara     — ghcr.io/pasta-devs/marinara-engine:latest
#   Lumiverse    — github.com/prolix-oc/Lumiverse (main)
# =============================================================================

FROM ghcr.io/sillytavern/sillytavern:latest AS sillytavern
FROM ghcr.io/pasta-devs/marinara-engine:latest AS marinara

FROM oven/bun:1-slim AS lumiverse-build
ARG LUMIVERSE_REPO=https://github.com/prolix-oc/Lumiverse.git
ARG LUMIVERSE_REF=main
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && git clone --depth 1 --branch "${LUMIVERSE_REF}" "${LUMIVERSE_REPO}" .

# HF reverse-proxy auth fix (x-forwarded-proto / x-forwarded-host)
RUN sed -i 's/c.req.header("host")/c.req.header("x-forwarded-host") || c.req.header("host")/g' src/app.ts \
    && sed -i 's/`http:\/\/${host}`/`${(c.req.header("x-forwarded-proto") || "http")}:\/\/${host}`/g' src/app.ts || true

WORKDIR /build/frontend
RUN bun install --frozen-lockfile 2>/dev/null || bun install && bun run build
WORKDIR /build
RUN bun install --production --frozen-lockfile 2>/dev/null || bun install --production

FROM node:24-bookworm-slim

LABEL org.opencontainers.image.source="https://github.com/Sexlovr/ai-hub-frontend"
LABEL org.opencontainers.image.title="AI Frontends Hub"
LABEL org.opencontainers.image.description="SillyTavern + Lumiverse + Marinara Engine on one HF Space"

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      nginx supervisor tini curl ca-certificates rsync inotify-tools python3 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /var/log/supervisor /run/nginx /data \
    && chown -R www-data:www-data /var/log/nginx /run/nginx

COPY --from=oven/bun:1-slim /usr/local/bin/bun /usr/local/bin/bun
COPY --from=oven/bun:1-slim /usr/local/bin/bunx /usr/local/bin/bunx

COPY --from=sillytavern /home/node/app /apps/sillytavern
COPY --from=marinara /app /apps/marinara
COPY --from=marinara /usr/local/bin/marinara-docker-entrypoint.mjs /usr/local/bin/marinara-docker-entrypoint.mjs
COPY --from=lumiverse-build /build /apps/lumiverse

# Hub scripts from this repo (HF clones Sexlovr/ai-hub-frontend as build context)
COPY docker/ /opt/hub/docker/
COPY scripts/ /opt/hub/scripts/
COPY config/ /opt/hub/config/
COPY public/ /opt/hub/public/

RUN chmod +x /opt/hub/docker/*.sh /opt/hub/scripts/*.sh \
    && chown -R node:node /apps/sillytavern /apps/marinara /data

ENV DATA_ROOT=/data
ENV HUB_PORT=7860
ENV ACTIVE_APP=sillytavern
ENV ST_PORT=8000
ENV LUMIVERSE_PORT=7861
ENV MARINARA_PORT=7862
ENV NODE_ENV=production
ENV TRUST_ANY_ORIGIN=true

VOLUME ["/data"]
EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=10s --start-period=180s --retries=5 \
  CMD curl -fsS "http://127.0.0.1:7870/api/health" || exit 1

ENTRYPOINT ["/opt/hub/docker/entrypoint.sh"]
CMD ["supervisord", "-n", "-c", "/opt/hub/docker/supervisord.conf"]