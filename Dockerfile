# =============================================================================
# AI Frontends Hub — Hugging Face Space (UID 1000 / node user, lightweight)
# Repo: https://github.com/lolmaobruhhh/ai-hub-frontend-test
# =============================================================================

FROM alpine:3.20 AS hub-src
RUN apk add --no-cache git \
    && git clone --depth 1 https://github.com/lolmaobruhhh/ai-hub-frontend-test.git /hub

FROM ghcr.io/sillytavern/sillytavern:latest AS sillytavern
FROM ghcr.io/pasta-devs/marinara-engine:lite AS marinara
FROM ghcr.io/prolix-oc/lumiverse:latest AS lumiverse

FROM node:24-bookworm-slim

# node:24-bookworm-slim already has `node` at UID 1000 (HF requirement)
RUN apt-get update && apt-get install -y --no-install-recommends \
      nginx python3 curl ca-certificates rsync git \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /tmp && chmod 777 /tmp

COPY --from=lumiverse /usr/local/bin/bun /usr/local/bin/bun
COPY --from=hub-src --chown=node:node /hub/docker /opt/hub/docker/
COPY --from=hub-src --chown=node:node /hub/scripts /opt/hub/scripts/
COPY --from=hub-src --chown=node:node /hub/config /opt/hub/config/
COPY --from=hub-src --chown=node:node /hub/public /opt/hub/public/
RUN cp /opt/hub/public/index.html /opt/hub/public/hub.html
COPY --from=sillytavern --chown=node:node /home/node/app /apps/sillytavern
COPY --from=marinara --chown=node:node /app /apps/marinara
COPY --from=lumiverse --chown=node:node /app /apps/lumiverse

RUN chmod +x /opt/hub/docker/*.sh /opt/hub/scripts/*.sh \
    && chmod +x /opt/hub/docker/start-all-apps.sh \
    && echo 'upstream active_backend { server 127.0.0.1:8000; }' > /opt/hub/docker/upstream.conf \
    && /opt/hub/docker/patch-lumiverse-auth.sh \
    && /opt/hub/docker/patch-app-subpaths.sh \
    && /opt/hub/docker/patch-lumiverse-sw.sh \
    && /opt/hub/docker/patch-marinara-sw.sh


# === Remove default ST character cards from the image (they live in /data/shared) ===
RUN rm -f /apps/sillytavern/default/content/default_Seraphina.png /apps/sillytavern/default/content/Seraphina 2>/dev/null; \
    rm -f /apps/sillytavern/default/content/*.png 2>/dev/null; \
    echo "[build] removed default ST character cards (shared-only mode)"

USER node
ENV HOME=/home/node
WORKDIR /home/node

ENV DATA_ROOT=/data
ENV HUB_PORT=7860
ENV ACTIVE_APP=sillytavern
ENV ST_PORT=8000
ENV LUMIVERSE_PORT=7861
ENV MARINARA_PORT=7862
ENV NODE_ENV=production
ENV TRUST_ANY_ORIGIN=true
ENV FORWARDED_PROTO=https

EXPOSE 7860

CMD ["bash", "/opt/hub/docker/start-hf.sh"]
