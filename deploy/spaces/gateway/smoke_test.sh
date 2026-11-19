#!/usr/bin/env bash
# Smoke test for the gateway Space image (CI "Space images build" job, or locally after a build):
#   bash deploy/spaces/gateway/smoke_test.sh [image]
# Boots the image with simulated inference, then checks: full boot to ready (migrate, seed, synthetic
# traffic); the local Redis and Postgres refuse unauthenticated clients; the gateway fails closed while OPA
# is stopped and recovers (bundle re-pushed) when it is back; a killed Redis is restarted; the shipped replay
# set loads; the database backup (db_backup.py, a bind-mounted `file:` bucket) dumps on stop, and the next
# container restores it and skips the synthetic boot traffic. The front door (ADR-0025): nginx owns the port
# and answers GATEWAY_STARTING while the gateway boots; the gateway listens on 127.0.0.1 only and refuses a
# request without the proxy proof; CORS answers exactly the configured origins. Everything goes through
# /gateway/ with the Space's settings (platform-proxy ingress, a proxy secret), as on the Space.
set -euo pipefail

IMAGE="${1:-aiva-gateway}"
NAME="aiva-gw-smoke-$$"
PORT="${SMOKE_PORT:-17860}"
BASE="http://127.0.0.1:$PORT/gateway"
UI_ORIGIN="https://s1.example.hf.space"
LAB_ORIGIN="https://s3.example.hf.space"
PROXY_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"

BACKUP_DIR="$(mktemp -d)"
chmod 777 "$BACKUP_DIR" # the container runs as uid 1000

cleanup() {
    if [ "${KEEP:-0}" != "1" ]; then
        docker rm -f "$NAME" >/dev/null 2>&1 || true
        # The dumps belong to the container's user (uid 1000), not the runner's: remove them as that user.
        docker run --rm --entrypoint sh -v "$BACKUP_DIR:/backup" "$IMAGE" -c 'rm -rf /backup/* /backup/.[!.]*' \
            >/dev/null 2>&1 || true
        rm -rf "$BACKUP_DIR" 2>/dev/null || true
    fi
}
trap cleanup EXIT
fail() {
    echo "SMOKE FAILED: $*" >&2
    docker logs --tail 80 "$NAME" >&2 || true
    exit 1
}
in_container() { docker exec -e AIVA_RUN_DIR=/tmp/aiva-run "$NAME" "$@"; }
ctl() { in_container supervisorctl -c /app/deploy/supervisord.conf "$@"; }
status_of() { curl -s -o /dev/null -w '%{http_code}' "$BASE$1" || true; }
wait_status() { # path expected-code seconds
    for _ in $(seq 1 "$3"); do
        [ "$(status_of "$1")" = "$2" ] && return 0
        sleep 1
    done
    fail "$1 did not return $2 within $3 s (last $(status_of "$1"))"
}

# The bucket directory is written by the container's user (uid 1000) with umask 077: read it as that user.
in_bucket() { docker run --rm --entrypoint "$1" -v "$BACKUP_DIR:/backup" "$IMAGE" "${@:2}"; }
in_db() { in_container env PGPASSFILE=/tmp/aiva-run/pgpass psql -h 127.0.0.1 -U aiva -d aiva -tAc "$1"; }
start() {
    docker run -d --name "$NAME" -p "$PORT:7860" \
        -e AIVA_INFERENCE_MODE=simulated -e AIVA_RLCD_BACKEND=heuristic -e AIVA_ENVIRONMENT=local \
        -e AIVA_DEV_SIGN_IN=true -e AIVA_BOOT_TRAFFIC_RUNS=20 -e AIVA_WATCHDOG_INTERVAL_SECONDS=2 \
        -e AIVA_INGRESS=platform-proxy -e AIVA_PROXY_SECRET="$PROXY_SECRET" \
        -e AIVA_PROXY_CORS_ORIGINS="$UI_ORIGIN $LAB_ORIGIN" \
        -v "$BACKUP_DIR:/backup" -e AIVA_BACKUP_BUCKET=file:/backup -e AIVA_BACKUP_INTERVAL_SECONDS=3600 \
        "$IMAGE" >/dev/null
}

# An empty bucket: the first boot is a fresh one, exactly as without backups.
start

# The front door is up within seconds and says the gateway is starting (retryable), not a connection error.
for _ in $(seq 1 60); do
    starting="$(curl -s "$BASE/v1/auth/sign-in-options" || true)"
    [ -n "$starting" ] && break
    sleep 1
done
grep -q '"code":"GATEWAY_STARTING"' <<<"$starting" || fail "no GATEWAY_STARTING while booting ($starting)"
echo "ok: front door up first; GATEWAY_STARTING while the gateway boots"

wait_status /health/ready 200 600
echo "ok: boot (migrate, seed, traffic) to ready"

# Only nginx is reachable: the gateway listens on 127.0.0.1 only (no 0.0.0.0 or :: listener on its port), so
# another host cannot connect to it, while the front door answers.
listeners="$(in_container sh -c 'cat /proc/net/tcp; cat /proc/net/tcp6 2>/dev/null || true' \
    | awk '$4 == "0A" {print $2}')" # tcp6 is absent where IPv6 is off
grep -qi ':1F40$' <<<"$listeners" || fail "the gateway is not listening on 8000"
grep -i ':1F40$' <<<"$listeners" | grep -qv '^0100007F:' && fail "the gateway listens beyond 127.0.0.1"
ip="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$NAME")"
peer() { docker run --rm --entrypoint curl "$IMAGE" -s -o /dev/null -w '%{http_code}' --max-time 5 "$1" || true; }
[ "$(peer "http://$ip:8000/health/live")" = 000 ] || fail "the gateway port is reachable from another host"
[ "$(peer "http://$ip:7860/gateway/health/live")" = 200 ] || fail "the front door is not reachable"
echo "ok: the gateway listens on 127.0.0.1 only; another host reaches the front door, never the gateway"

# A request that reaches the gateway without the proxy proof is refused, as behind the Space 1/3 proxies.
direct="$(in_container curl -s http://127.0.0.1:8000/v1/auth/dev/users)"
grep -q '"code":"PROXY_PROOF_REQUIRED"' <<<"$direct" || fail "the gateway served a request without the proof"
echo "ok: without the proxy proof the gateway refuses (PROXY_PROOF_REQUIRED)"

# CORS: exactly the configured origins, on preflights (answered by nginx) and on answers.
preflight() {
    curl -s -D - -o /dev/null -X OPTIONS "$BASE/v1/chat" -H "Origin: $1" \
        -H "Access-Control-Request-Method: POST" \
        -H "Access-Control-Request-Headers: x-aiva-authorization,content-type"
}
grep -qi "^access-control-allow-origin: $UI_ORIGIN" <<<"$(preflight "$UI_ORIGIN")" \
    || fail "the Control Center's preflight was not allowed"
lab_allowed="$(grep -i '^access-control-allow-headers:' <<<"$(preflight "$LAB_ORIGIN")" || true)"
grep -qi 'x-aiva-authorization' <<<"$lab_allowed" \
    || fail "the Context Lab's preflight does not allow X-AiVa-Authorization"
# A browser sends the AiVa token only in X-AiVa-Authorization; a bare Authorization is never allowed.
grep -qiE '(:|,) *authorization( *,|\s*$)' <<<"$lab_allowed" && fail "the preflight allows Authorization"
grep -qi '^access-control-allow-origin' <<<"$(preflight https://elsewhere.example)" \
    && fail "an unlisted origin was allowed"
answer="$(curl -s -D - -o /dev/null "$BASE/v1/auth/dev/users" -H "Origin: $LAB_ORIGIN")"
grep -q '^HTTP/1.1 200' <<<"$answer" && grep -qi "^access-control-allow-origin: $LAB_ORIGIN" <<<"$answer" \
    || fail "an allowed origin's answer lacks Access-Control-Allow-Origin"
# Exactly one Access-Control-Allow-Origin (the gateway adds none; HF's edge adds one only when the app sends
# none, verified 2026-10-22), on an answer, a preflight and the proxy's own JSON 429.
acao_count() { grep -ci '^access-control-allow-origin:' <<<"$1" || true; }
[ "$(acao_count "$answer")" = 1 ] || fail "an answer carries $(acao_count "$answer") Access-Control-Allow-Origin"
[ "$(acao_count "$(preflight "$UI_ORIGIN")")" = 1 ] || fail "a preflight does not carry exactly one ACAO"
grep -qi '^access-control-allow-headers: x-aiva-authorization, content-type, idempotency-key' \
    <<<"$(preflight "$UI_ORIGIN")" || fail "the preflight does not allow exactly X-AiVa-Authorization, Content-Type, Idempotency-Key"
burst_dir="$(mktemp -d)"
for i in $(seq 1 40); do
    curl -s -D "$burst_dir/$i" -o /dev/null -H "Origin: $UI_ORIGIN" -H "X-Forwarded-For: 8.8.8.8" \
        "$BASE/health/live" &
done
wait
limited_file="$(grep -l '^HTTP/1.1 429' "$burst_dir"/* | head -1 || true)"
[ -n "$limited_file" ] || fail "no 429 under a burst of 40"
[ "$(acao_count "$(cat "$limited_file")")" = 1 ] || fail "the proxy's 429 does not carry exactly one ACAO"
rm -rf "$burst_dir"
echo "ok: CORS for exactly the Space 1 and Space 3 origins; exactly one ACAO, also on the proxy's 429"

# The API description stays off the public front door.
for path in /docs /redoc /openapi.json; do
    [ "$(status_of "$path")" = 404 ] || fail "$path is served on the front door"
done
echo "ok: /docs, /redoc and /openapi.json are not served"

# Client-sent proof and addresses are overwritten (Govern, ADR-0025): the rightmost X-Forwarded-For entry (the
# hop HF's edge appends) keys a public session, never a spoofed X-AiVa-Client-IP, and it is attested (a cli_
# reference, not "unattested", which would put every visitor in one bucket).
session_from() { # rightmost-address spoofed-client-ip
    curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/v1/auth/public/session" \
        -H "X-Forwarded-For: 198.51.100.7, $1" -H "X-AiVa-Client-IP: $2" -H "X-AiVa-Proxy-Secret: forged-not-the-secret"
}
[ "$(session_from 1.1.1.1 9.9.9.9)" = 201 ] || fail "no public session through the front door"
[ "$(session_from 1.1.1.1 149.112.112.112)" = 201 ] || fail "no second public session"
[ "$(session_from 1.0.0.1 9.9.9.9)" = 201 ] || fail "no third public session"
sources="$(in_db "select details->>'source' from audit_log where action = 'auth.public_session' order by id" \
    | tail -3)"
[ "$(wc -l <<<"$sources")" = 3 ] || fail "expected three public-session audit rows, got: $sources"
first="$(sed -n 1p <<<"$sources")"; second="$(sed -n 2p <<<"$sources")"; third="$(sed -n 3p <<<"$sources")"
[[ "$first" == cli_* ]] || fail "the public session's source is $first, not an attested cli_ reference"
[ "$first" = "$second" ] || fail "a spoofed X-AiVa-Client-IP changed the source"
[ "$first" != "$third" ] || fail "different visitors share one source"
echo "ok: spoofed proof and client IP overwritten; the rightmost X-Forwarded-For keys an attested cli_ source"

# No ambient credentials (Govern, ADR-0025): the gateway reads the AiVa token from a header only. The same
# token in a cookie or the query string is no credential at all.
token="$(curl -s -X POST "$BASE/v1/auth/dev/token" -H 'Content-Type: application/json' \
    -d "{\"user_id\": \"$(curl -s "$BASE/v1/auth/dev/users" | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["user_id"])')\"}" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')"
[ -n "$token" ] || fail "no dev token"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/v1/auth/me" -H "X-AiVa-Authorization: Bearer $token")" = 200 ] \
    || fail "the token in X-AiVa-Authorization was not accepted"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/v1/auth/me" -H "Cookie: access_token=$token; token=$token")" = 401 ] \
    || fail "a token in a cookie was accepted"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/v1/auth/me?access_token=$token&token=$token")" = 401 ] \
    || fail "a token in the query string was accepted"
echo "ok: a token in a cookie or the query string is no credential; only the header counts"

# The shipped replay set loads (ADR-0019): every recording parses and is named after its fingerprint. An
# empty set passes until the recordings land.
in_container sh -c '[ "$AIVA_RECORDINGS_DIR" = /app/deploy/recordings ]' \
    || fail "AIVA_RECORDINGS_DIR is not the shipped replay set"
replay_set="$(in_container python /app/deploy/check_recordings.py)" || fail "the replay set does not load"
echo "ok: $replay_set"
# Capture first: with pipefail, `docker logs | grep -q` fails when grep exits early (SIGPIPE).
logs="$(docker logs "$NAME" 2>&1)"
grep -q "Generated 20 synthetic runs" <<<"$logs" || fail "boot traffic did not run"
echo "ok: synthetic traffic generated"
grep -q "^Ingested [0-9]* documents" <<<"$logs" || fail "knowledge base not ingested"
echo "ok: knowledge base ingested"
[ "$(status_of /v1/auth/dev/users)" = 200 ] || fail "dev sign-in users"
echo "ok: demo sign-in"

grep -q NOAUTH <<<"$(in_container redis-cli -h 127.0.0.1 --no-auth-warning ping 2>&1)" \
    || fail "redis accepted an unauthenticated client"
if in_container env -u PGPASSFILE psql -h 127.0.0.1 -U aiva -d aiva -w -c 'select 1' >/dev/null 2>&1; then
    fail "postgres accepted a client without a password"
fi
echo "ok: local Redis and Postgres require their generated passwords"

ctl stop opa >/dev/null
wait_status /health/ready 503 30
echo "ok: OPA down -> not ready (fail closed)"
ctl start opa >/dev/null
wait_status /health/ready 200 60
echo "ok: OPA restarted -> policy bundle re-pushed, ready"

in_container sh -c 'kill -9 "$(cat /proc/[0-9]*/stat 2>/dev/null | awk "\$2 == \"(redis-server)\" {print \$1}" | head -1)"'
for _ in $(seq 1 20); do # restarted, then RUNNING after supervisord's startsecs
    grep -q RUNNING <<<"$(ctl status redis)" && break
    sleep 1
done
grep -q RUNNING <<<"$(ctl status redis)" || fail "redis not restarted"
grep -q "exited: redis (terminated by SIGKILL" <<<"$(docker logs "$NAME" 2>&1)" || fail "redis was not killed"
wait_status /health/ready 200 60
echo "ok: killed Redis restarted by the supervisor"

# Database backup (db_backup.py): a check right after boot uploads the fresh database (an empty bucket allows
# it). The interval is an hour, so a change made now reaches the bucket only through the dump taken on stop:
# supervisord stops the backup program first, while Postgres still runs.
for _ in $(seq 1 60); do
    grep -q "db-backup: backed up aiva-" <<<"$(docker logs "$NAME" 2>&1)" && break
    sleep 1
done
grep -q "db-backup: backed up aiva-" <<<"$(docker logs "$NAME" 2>&1)" || fail "no backup right after boot"
echo "ok: database backed up right after boot"
in_db "insert into audit_log (tenant_id, actor_kind, actor_id, action, target_type, target_id, details, \
created_at) values ('gct', 'system', 'smoke', 'smoke.after-boot', 'backup', 's1', '{}', now())" >/dev/null
runs_before="$(in_db "select count(*) from execution_run")"
[ "$runs_before" -ge 20 ] || fail "expected the boot traffic's runs, found $runs_before"
docker stop -t 60 "$NAME" >/dev/null
logs="$(docker logs "$NAME" 2>&1)"
[ "$(grep -c "db-backup: backed up aiva-" <<<"$logs")" -ge 2 ] || fail "no database backup when the container stopped"
in_bucket test -s /backup/gateway/latest.json || fail "no backup manifest in the bucket"
echo "ok: database backed up again when the container stopped"

# A new container (a Space restart): restore before migrate, no second batch of boot traffic.
docker rm -f "$NAME" >/dev/null
start
wait_status /health/ready 200 600
logs="$(docker logs "$NAME" 2>&1)"
grep -q "db-backup: restored aiva-" <<<"$logs" || fail "the backup was not restored on boot"
grep -q "Generated 20 synthetic runs" <<<"$logs" && fail "boot traffic ran again on a restored database"
[ "$(in_db "select count(*) from execution_run")" = "$runs_before" ] || fail "runs not carried over"
[ "$(in_db "select count(*) from audit_log where action = 'smoke.after-boot'")" = 1 ] \
    || fail "the change made after boot did not survive (the stop-time dump was not restored)"
echo "ok: restored on boot ($runs_before runs and the stop-time change kept), boot traffic skipped, ready"

# An unreachable bucket never blocks boot: restoring into an empty database logs it and exits 0.
in_db "create database scratch" >/dev/null
out="$(in_container env PGHOST=127.0.0.1 PGUSER=aiva PGPASSFILE=/tmp/aiva-run/pgpass \
    AIVA_BACKUP_DATABASE=scratch AIVA_BACKUP_BUCKET=file:/no-such-mount python /app/deploy/db_backup.py restore)" \
    || fail "restore failed instead of falling back"
grep -q "bucket unavailable" <<<"$out" || fail "an unreachable bucket was not reported ($out)"
echo "ok: unreachable bucket -> logged, fresh database, boot continues"
echo "gateway image smoke test passed"
