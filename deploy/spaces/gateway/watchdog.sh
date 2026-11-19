#!/bin/sh
# Polls the gateway's readiness probe. Besides reporting, the probe repairs one failure mode: after OPA
# restarts it holds no policy bundle, and /health/ready makes the gateway re-push the published one.
# Until then policy evaluation fails closed. Logs state changes only.
set -u

interval="${AIVA_WATCHDOG_INTERVAL_SECONDS:-15}"
last=""
while :; do
    sleep "$interval"
    [ -f "$AIVA_RUN_DIR/boot.done" ] || continue
    body="$(curl -s --max-time 10 http://127.0.0.1:${AIVA_GATEWAY_PORT:-8000}/health/ready 2>/dev/null || true)"
    case "$body" in
        *'"status":"ok"'*) state=ready ;;
        "") state="unreachable" ;;
        *) state="degraded $body" ;;
    esac
    if [ "$state" != "$last" ]; then
        echo "watchdog: gateway $state"
        last="$state"
    fi
done
