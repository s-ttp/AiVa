#!/bin/sh
# Gateway program under supervisord. The first start in a container runs the boot sequence:
#   wait for Postgres, Redis and OPA -> create the database -> restore the last backup (if any) -> migrate ->
#   seed (config/ into Postgres,
#   policy bundle into OPA) -> knowledge base -> fetch the RLCD model -> optional synthetic traffic -> uvicorn.
# Later restarts (after a crash) go straight to uvicorn. A failed boot step stops the container.
set -eu

RUN_DIR="$AIVA_RUN_DIR"
log() { echo "gateway-boot: $*"; }
fail() {
    log "FAILED: $*; stopping the container"
    kill -TERM "$PPID" 2>/dev/null || true # supervisord, so the Space restarts us
    exit 1
}

wait_for() {
    name="$1"; shift
    i=0
    until "$@" >/dev/null 2>&1; do
        i=$((i + 1))
        [ "$i" -le "${AIVA_BOOT_WAIT_SECONDS:-120}" ] || fail "$name not ready"
        sleep 1
    done
    log "$name ready"
}

if [ ! -f "$RUN_DIR/boot.done" ]; then
    wait_for postgres pg_isready -h 127.0.0.1 -p 5432
    wait_for redis redis-cli -h 127.0.0.1 -p 6379 ping
    wait_for opa curl -sf http://127.0.0.1:8181/health

    if ! psql -h 127.0.0.1 -U aiva -d postgres -tAc "select 1 from pg_database where datname = 'aiva'" \
        | grep -q 1; then
        createdb -h 127.0.0.1 -U aiva aiva || fail "createdb"
        log "created database aiva"
    fi

    # Persisted database (db_backup.py): the newest good dump from the private backup bucket, into the empty
    # database, before migrate runs forward on it. A restored database is not fresh, so the synthetic boot
    # traffic below is skipped. It never blocks boot: without AIVA_BACKUP_BUCKET, or with the bucket
    # unreachable, the database starts fresh as before.
    if [ -n "${AIVA_BACKUP_BUCKET:-}" ]; then
        timeout "${AIVA_BACKUP_RESTORE_TIMEOUT_SECONDS:-300}" python /app/deploy/db_backup.py restore \
            || log "database restore did not finish; starting on a fresh database"
    fi

    if [ "${AIVA_RUN_MIGRATIONS:-true}" = "true" ]; then
        aiva-admin migrate || fail "migrate"
    fi
    # Re-seeding on every boot is what brings config/ changes (tools, tenant, policies) into the
    # database and OPA; without it new tools stay denied as unregistered.
    if [ "${AIVA_SEED_ON_START:-true}" = "true" ]; then
        aiva-admin seed || fail "seed"
    fi

    # Retrieval knowledge base for the CIM-assisted path (replaces the tenant's documents; idempotent).
    if [ -n "${AIVA_BOOT_KB:-}" ]; then
        aiva-admin kb-ingest "$AIVA_BOOT_KB" || fail "kb-ingest $AIVA_BOOT_KB"
    fi

    # RLCD model: fetched with HF_TOKEN into the data directory and loaded from there. Without it the
    # gateway runs the labelled heuristic (heuristic-v0), never pretending to be the model.
    if [ "${AIVA_RLCD_BACKEND:-heuristic}" = "model" ] && [ -z "${AIVA_RLCD_LOCAL_DIR:-}" ]; then
        rlcd_dir="${AIVA_DATA_DIR:-/tmp/aiva-data}/models/aiva-rlcd-modernbert"
        if python /app/deploy/prefetch_models.py rlcd "$rlcd_dir"; then
            echo "$rlcd_dir" > "$RUN_DIR/rlcd-dir"
        else
            log "RLCD model unavailable; the heuristic RLCD serves (recorded as heuristic-v0)"
        fi
    fi

    # Synthetic Insights traffic (ADR-0015): simulated inference only, so it spends nothing and every
    # number it produces is labelled simulated/synthetic. Only into a freshly created database, never into
    # a restored one.
    runs="${AIVA_BOOT_TRAFFIC_RUNS:-60}"
    if [ "$runs" -gt 0 ] && [ -f "$RUN_DIR/fresh-database" ]; then
        AIVA_INFERENCE_MODE=simulated AIVA_RLCD_BACKEND=heuristic aiva-admin traffic --runs "$runs" \
            || log "synthetic traffic failed; Insights starts empty"
    fi
    touch "$RUN_DIR/boot.done"
    log "boot complete"
fi

if [ -s "$RUN_DIR/rlcd-dir" ]; then
    AIVA_RLCD_LOCAL_DIR="$(cat "$RUN_DIR/rlcd-dir")"
    export AIVA_RLCD_LOCAL_DIR
fi

# Loopback only: nginx (front-door.sh) owns the Space port and is the only way in (ADR-0025).
# --no-proxy-headers: the gateway resolves client IPs itself from AIVA_TRUSTED_PROXIES (ADR-0007).
exec uvicorn --factory aiva_gateway.main:create_app --host 127.0.0.1 --port "${AIVA_GATEWAY_PORT:-8000}" \
    --no-proxy-headers
