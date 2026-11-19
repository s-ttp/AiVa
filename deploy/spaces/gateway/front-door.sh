#!/bin/sh
# nginx program under supervisord: the gateway's public front door on the Space port (ADR-0025). It writes
# the /gateway/ location and the CORS map from AIVA_GATEWAY_UPSTREAM, AIVA_PROXY_SECRET and
# AIVA_PROXY_CORS_ORIGINS (gateway-proxy.sh), checks the configuration, then runs nginx in the foreground.
# It starts before the gateway finishes booting, so callers get the retryable GATEWAY_STARTING answer
# instead of a connection error while the database restores and migrates.
set -eu

mkdir -p /tmp/aiva-nginx
/app/deploy/gateway-proxy.sh /tmp/aiva-nginx/gateway-proxy.conf /tmp/aiva-nginx/gateway-cors.conf
nginx -e /dev/stderr -t -q -c /app/deploy/nginx.conf
exec nginx -e /dev/stderr -c /app/deploy/nginx.conf -g 'daemon off;'
