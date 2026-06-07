# =============================================================================
# AI Frontends Hub — Hugging Face Space (UID 1000, no root, lightweight build)
# Repo: https://github.com/Sexlovr/ai-hub-frontend
# =============================================================================

FROM alpine:3.20 AS hub-src
RUN apk add --no-cache git \
    && git clone --depth 1 https://github.com/Sexlovr/ai-hub-frontend.git /hub

FROM ghcr.io/sillytavern/sillytavern:latest AS sillytavern
FROM ghcr.io/pasta-devs/marinara-engine:lite AS marinara

FROM oven/bun:1-slim AS bun-bin

FROM node:24-bookworm-slim

# HF Spaces always run containers as UID 1000
RUN useradd -m -u 1000 user

RUN apt-get update && apt-get install -y --no-install-recommends \
      nginx python3 curl ca-certificates git rsync \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /tmp && chmod 777 /tmp

COPY --from=bun-bin /usr/local/bin/bun /usr/local/bin/bun
COPY --from=hub-src --chown=user:user /hub/docker /opt/hub/docker/
COPY --from=hub-src --chown=user:user /hub/scripts /opt/hub/scripts/
COPY --from=hub-src --chown=user:user /hub/config /opt/hub/config/
COPY --from=hub-src --chown=user:user /hub/public /opt/hub/public/
COPY --from=sillytavern --chown=user:user /home/node/app /apps/sillytavern
COPY --from=marinara --chown=user:user /app /apps/marinara

# Lumiverse cloned at runtime on first switch (saves ~5GB build disk)
RUN git clone --depth 1 https://github.com/prolix-oc/Lumiverse.git /apps/lumiverse-src \
    && chown -R user:user /apps/lumiverse-src /opt/hub /apps

RUN chmod +x /opt/hub/docker/*.sh /opt/hub/scripts/*.sh

USER user
ENV HOME=/home/user
WORKDIR /home/user

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

# Foreground process for HF — logs go to stderr immediately
CMD ["bash", "/opt/hub/docker/start-hf.sh"]