# syntax=docker/dockerfile:1
#
# AiVa Control Center: Hugging Face Docker Space (technical name gct-ai-control-center).
# Build context is the repository root:
#   docker build -f deploy/spaces/control-center/Dockerfile -t aiva-control-center .
# The gateway URL is read at container start from AIVA_GATEWAY_URL, so one image
# serves every environment.

FROM node:22.23.3-trixie-slim AS build
ENV CI=true
RUN npm install --global pnpm@10.34.6
WORKDIR /app/web

# Fetch dependencies from the lockfile alone so this layer survives source edits.
COPY web/pnpm-lock.yaml web/pnpm-workspace.yaml ./
RUN pnpm fetch

COPY web/ ./
RUN pnpm install --offline --frozen-lockfile --filter "@aiva/control-center..." \
 && pnpm --filter @aiva/control-center build

FROM nginxinc/nginx-unprivileged:1.30.4-alpine3.24

# Hugging Face Spaces run containers as uid 1000. Static files stay root-owned
# and read-only; only the runtime-config directory is writable.
USER root
RUN apk add --no-cache ca-certificates \
 && adduser -D -H -u 1000 -s /sbin/nologin aiva \
 && rm /etc/nginx/conf.d/default.conf \
 && mkdir -p /var/run/aiva \
 && chown aiva:aiva /var/run/aiva

COPY deploy/spaces/shared/aiva-http.conf /etc/nginx/conf.d/00-aiva-http.conf
COPY deploy/spaces/control-center/nginx.conf /etc/nginx/conf.d/aiva.conf
COPY --chmod=0755 deploy/spaces/control-center/40-aiva-runtime-config.sh /docker-entrypoint.d/
COPY --chmod=0755 deploy/spaces/control-center/41-aiva-gateway-proxy.sh /docker-entrypoint.d/
COPY --chmod=0755 deploy/spaces/shared/gateway-proxy.sh /usr/local/bin/aiva-gateway-proxy
COPY --from=build /app/web/apps/control-center/dist/ /usr/share/nginx/html/
# Served from /var/run/aiva instead; never ship the development default.
RUN rm /usr/share/nginx/html/config.js

USER 1000
EXPOSE 7860
