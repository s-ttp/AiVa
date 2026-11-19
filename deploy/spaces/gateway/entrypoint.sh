#!/bin/sh
# HF Space 2 (gct-rlcd-gateway): prepare the data directory and credentials for the co-located Postgres
# and Redis, then hand over to supervisord, which runs Postgres, Redis, OPA, the gateway and a readiness
# watchdog. Anything that stays FATAL stops the container so the Space restarts it.
#
# AIVA_DATA_DIR (default /tmp/aiva-data) holds the Postgres cluster, the local service passwords and the
# RLCD model. The Space disk is ephemeral; with AIVA_BACKUP_BUCKET set, db_backup.py restores the database
# from a private HF Storage Bucket on boot and backs it up while running (deploy/RUNBOOK.md).
set -eu

DATA_DIR="${AIVA_DATA_DIR:-/tmp/aiva-data}"
export AIVA_PGDATA="${AIVA_PGDATA:-$DATA_DIR/postgres}"
export AIVA_RUN_DIR="${AIVA_RUN_DIR:-/tmp/aiva-run}"
SECRETS="$DATA_DIR/secrets"
umask 077
mkdir -p "$DATA_DIR" "$SECRETS" "$AIVA_RUN_DIR"
rm -f "$AIVA_RUN_DIR"/*.done "$AIVA_RUN_DIR/fresh-database" "$AIVA_RUN_DIR/rlcd-dir" "$AIVA_RUN_DIR/restored" \
    "$AIVA_RUN_DIR/backup-ok"

# Local service passwords: generated once per data directory, never printed. The services listen on
# 127.0.0.1 only; the passwords keep other local code (e.g. a document render) off them.
secret() {
    [ -s "$SECRETS/$1" ] || python -c "import secrets; print(secrets.token_urlsafe(32))" > "$SECRETS/$1"
    cat "$SECRETS/$1"
}
PG_PASSWORD="$(secret postgres)"
REDIS_PASSWORD="$(secret redis)"

if [ ! -s "$AIVA_PGDATA/PG_VERSION" ]; then
    mkdir -p "$AIVA_PGDATA"
    initdb -D "$AIVA_PGDATA" -U aiva --pwfile="$SECRETS/postgres" --auth-local=scram-sha-256 \
        --auth-host=scram-sha-256 --encoding=UTF8 --locale=C.UTF-8 >/dev/null
    touch "$AIVA_RUN_DIR/fresh-database"
    echo "entrypoint: initialised an empty Postgres cluster in $AIVA_PGDATA"
fi

# Redis takes its password from a config file so it never appears in a process listing.
printf 'bind 127.0.0.1\nport 6379\nprotected-mode yes\nrequirepass %s\nsave ""\nappendonly no\n' \
    "$REDIS_PASSWORD" > "$AIVA_RUN_DIR/redis.conf"

# The gateway and aiva-admin read these; an operator-provided URL (external services) wins.
export AIVA_DATABASE_URL="${AIVA_DATABASE_URL:-postgresql+asyncpg://aiva:$PG_PASSWORD@127.0.0.1:5432/aiva}"
export AIVA_REDIS_URL="${AIVA_REDIS_URL:-redis://:$REDIS_PASSWORD@127.0.0.1:6379/0}"
export AIVA_OPA_URL="${AIVA_OPA_URL:-http://127.0.0.1:8181}"
export PGPASSFILE="$AIVA_RUN_DIR/pgpass"
# libpq defaults for the local cluster (db_backup.py's pg_dump, pg_restore and psql).
export PGHOST=127.0.0.1 PGPORT=5432 PGUSER=aiva
printf '127.0.0.1:5432:*:aiva:%s\n' "$PG_PASSWORD" > "$PGPASSFILE"
export REDISCLI_AUTH="$REDIS_PASSWORD"
unset PG_PASSWORD REDIS_PASSWORD

exec supervisord -n -c /app/deploy/supervisord.conf
