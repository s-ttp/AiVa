#!/usr/bin/env bash
# Start Postgres (pgvector), Redis and OPA locally without Docker — for Claude Code cloud sessions and
# other machines where `docker compose` is unavailable. Idempotent; safe to re-run.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OPA_VERSION="${OPA_VERSION:-1.20.0}"
PG_BIN="${PG_BIN:-/usr/lib/postgresql/16/bin}"
PGDATA="${PGDATA:-/var/lib/aiva-pg}"
STATE="${AIVA_DEV_STATE:-$HOME/.aiva-dev}"
mkdir -p "$STATE" "$HOME/.local/bin"
export PATH="$HOME/.local/bin:$PATH"

if [ ! -x "$PG_BIN/postgres" ]; then
    echo "PostgreSQL 16 server binaries not found in $PG_BIN" >&2
    exit 1
fi
if [ ! -f /usr/share/postgresql/16/extension/vector.control ]; then
    apt-get install -y -q postgresql-16-pgvector >/dev/null 2>&1 || {
        apt-get update -q >/dev/null 2>&1 && apt-get install -y -q postgresql-16-pgvector >/dev/null
    }
fi
if [ ! -f "$PGDATA/PG_VERSION" ]; then
    mkdir -p "$PGDATA" && chown postgres:postgres "$PGDATA"
    su postgres -c "$PG_BIN/initdb -D $PGDATA -A trust -U postgres" >/dev/null
fi
if ! su postgres -c "$PG_BIN/pg_ctl -D $PGDATA status" >/dev/null 2>&1; then
    su postgres -c "$PG_BIN/pg_ctl -D $PGDATA -l $PGDATA/server.log -o '-p 5432 -k /tmp' start" >/dev/null
    sleep 2
fi
psql -h 127.0.0.1 -U postgres -tAqc "select 1 from pg_roles where rolname='aiva'" | grep -q 1 \
    || psql -h 127.0.0.1 -U postgres -qc "create role aiva login password 'aiva' superuser"
for db in aiva aiva_test; do
    psql -h 127.0.0.1 -U postgres -tAqc "select 1 from pg_database where datname='$db'" | grep -q 1 \
        || psql -h 127.0.0.1 -U postgres -qc "create database $db owner aiva"
done

redis-cli ping >/dev/null 2>&1 || redis-server --daemonize yes --port 6379 --dir "$STATE" >/dev/null

if ! opa version 2>/dev/null | grep -q "Version: $OPA_VERSION"; then
    curl -sSL -o "$HOME/.local/bin/opa" "https://openpolicyagent.org/downloads/v$OPA_VERSION/opa_linux_amd64_static"
    chmod +x "$HOME/.local/bin/opa"
fi
if ! curl -sf http://127.0.0.1:8181/health >/dev/null; then
    # setsid detaches OPA so it outlives the shell that started it.
    setsid nohup opa run --server --watch --addr 127.0.0.1:8181 --log-level error \
        "$ROOT/policies/aiva/decision.rego" >"$STATE/opa.log" 2>&1 </dev/null &
    disown || true
    sleep 2
fi

echo "postgres: $(pg_isready -h 127.0.0.1 -p 5432 | cut -d- -f2)"
echo "redis:    $(redis-cli ping)"
echo "opa:      $(curl -s http://127.0.0.1:8181/health)"
