#!/bin/sh
# Writes the nginx `location /gateway/` block, the gateway's public front door (ADR-0019, ADR-0025). Two modes:
# - Front door (Space 2): nginx in the gateway's own container proxies to the gateway on the loopback
#   (AIVA_GATEWAY_UPSTREAM=http://127.0.0.1:<port>). The Space is public and the UIs of Space 1 and Space 3
#   call it from the browser, so it answers CORS for exactly AIVA_PROXY_CORS_ORIGINS. No HF token.
# - Hop (Space 1 and Space 3 until the cutover, and the rollback): server-side, to the private gateway Space
#   over HF's edge with an HF token, so the browser talks to one origin and never sees the token.
#
#   gateway-proxy.sh <location file> [<http-level CORS map file>]
#
# Environment:
#   AIVA_GATEWAY_UPSTREAM  http://127.0.0.1:<port> (front door), or https://<owner>-gct-rlcd-gateway.hf.space
#   HF_TOKEN               hop only, Space secret: a fine-grained token with read access to the gateway Space
#   AIVA_PROXY_SECRET      secret shared with the gateway: proves the request came through a proxy
#   AIVA_PROXY_CORS_ORIGINS  front door only: space-separated https:// origins allowed to call it from a
#                          browser; written as a `map` into the second file (http context)
#   AIVA_PROXY_ALLOW_HTTP_UPSTREAM=1   tests only: allow another http:// upstream (the smoke test's stub)
#
# Upstream headers follow Govern's ingress spec (ADR-0019). They are an explicit whitelist
# (proxy_pass_request_headers off):
#   Authorization: Bearer $HF_TOKEN       hop only: the private Space's access
#   X-AiVa-Authorization                  the client's AiVa credential, only when it sent one: its own
#                                         X-AiVa-Authorization, else its Authorization; never an HF token
#                                         (`hf_…`, with or without `Bearer`). A request without one is the
#                                         public visitor; the proxy never invents one
#   X-AiVa-Proxy-Secret                   $AIVA_PROXY_SECRET
#   X-AiVa-Client-IP                      the rightmost X-Forwarded-For entry: the hop HF's edge appends
#                                         (verified 2026-10-15: a client-sent value stays on the left)
#   Content-Type, Content-Length, Accept, Idempotency-Key
# Everything else the client sends, including any other X-AiVa-*, X-Forwarded-*, X-Real-IP and cookies, is
# dropped by construction. Upstream Set-Cookie and HF routing headers are not passed to the browser.
# $aiva_credential, $aiva_client_ip and the aiva_gateway rate-limit zone come from the http-level aiva-http.conf.
# Without an upstream, token or proxy secret the location answers 503 and forwards nothing. When the
# gateway Space is unreachable (502/504, connect failure) the browser gets a retryable "starting" answer:
# JSON (code GATEWAY_STARTING) for API calls, an auto-refreshing HTML page for navigations.
set -eu

out="$1"
cors_out="${2:-}"
upstream="${AIVA_GATEWAY_UPSTREAM:-}"
token="${HF_TOKEN:-}"
proxy_secret="${AIVA_PROXY_SECRET:-}"
origins="${AIVA_PROXY_CORS_ORIGINS:-}"

# The map exists whenever its file is asked for, so the http-level include never fails: no origin matches
# until the checks below have passed.
cors_map() {
    umask 022
    {
        echo "# Generated at container start by gateway-proxy.sh: browser origins allowed to call /gateway/."
        echo 'map $http_origin $aiva_cors_origin {'
        echo '    default "";'
        for origin in $1; do echo "    \"$origin\" \$http_origin;"; done
        echo '}'
    } > "$cors_out"
}
[ -z "$cors_out" ] || cors_map ""

unavailable() {
    echo "$0: $1; /gateway/ answers 503" >&2
    cat > "$out" <<'EOF'
location /gateway/ {
    default_type application/json;
    add_header Cache-Control "no-store" always;
    return 503 '{"error":{"code":"GATEWAY_PROXY_NOT_CONFIGURED","message":"The gateway proxy is not configured."}}';
}
EOF
    exit 0
}

[ -n "$upstream" ] || unavailable "AIVA_GATEWAY_UPSTREAM is not set"
loopback=0
case "$upstream" in
    https://*) ;;
    http://127.0.0.1:[0-9]*) loopback=1 ;; # the front door: the gateway in the same container
    http://*) [ "${AIVA_PROXY_ALLOW_HTTP_UPSTREAM:-}" = "1" ] || {
        echo "$0: AIVA_GATEWAY_UPSTREAM must be https://, or http://127.0.0.1:<port>" >&2
        exit 1
    } ;;
    *) echo "$0: AIVA_GATEWAY_UPSTREAM must be an https:// origin" >&2; exit 1 ;;
esac
upstream="${upstream%/}"
host="${upstream#*://}"
# An origin only (scheme, host, optional port), from a strict character set: it is written into nginx
# configuration, so nothing that could end a directive may pass.
case "$host" in
    "" | */* | *[!A-Za-z0-9.:-]*) echo "$0: AIVA_GATEWAY_UPSTREAM must be an origin like https://host" >&2; exit 1 ;;
esac
# The hop authenticates to the private Space with the HF token; the front door needs none and sends none.
upstream_auth=""
upstream_tls=""
if [ "$loopback" = 0 ]; then
    [ -n "$token" ] || unavailable "HF_TOKEN is not set"
    case "$token" in
        *[!A-Za-z0-9_-]*) echo "$0: HF_TOKEN has unexpected characters" >&2; exit 1 ;;
    esac
    upstream_auth="    proxy_set_header Authorization \"Bearer $token\";"
    upstream_tls="    proxy_ssl_server_name on;
    proxy_ssl_verify on;
    proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
    proxy_ssl_verify_depth 4;"
fi
[ -n "$proxy_secret" ] || unavailable "AIVA_PROXY_SECRET is not set"
case "$proxy_secret" in
    *[!A-Za-z0-9_-]*) echo "$0: AIVA_PROXY_SECRET may only contain A-Z a-z 0-9 _ -" >&2; exit 1 ;;
esac
[ "${#proxy_secret}" -ge 32 ] || { echo "$0: AIVA_PROXY_SECRET must be at least 32 characters" >&2; exit 1; }

# CORS (front door): exactly the listed origins get Access-Control-Allow-Origin, on every answer including
# the proxy's own 429 and "starting" 503, so the browser can read them. Preflights are answered here, before
# the rate limit and without reaching the gateway. The UIs send the AiVa token only in X-AiVa-Authorization, never
# Authorization (which a platform edge reads), and no cookies, so credentials stay off. On HF this is not a
# security boundary: its edge answers preflights itself and reflects any origin (ADR-0025, verified 2026-10-22).
cors=""
cors_headers=""
if [ -n "$origins" ]; then
    [ -n "$cors_out" ] || { echo "$0: AIVA_PROXY_CORS_ORIGINS needs the CORS map file argument" >&2; exit 1; }
    for origin in $origins; do
        case "$origin" in
            https://*) ;;
            http://localhost:[0-9]* | http://127.0.0.1:[0-9]*) ;; # local stacks (compose)
            *) echo "$0: AIVA_PROXY_CORS_ORIGINS entries must be https:// origins" >&2; exit 1 ;;
        esac
        case "${origin#*://}" in
            "" | */* | *[!A-Za-z0-9.:-]*) echo "$0: AIVA_PROXY_CORS_ORIGINS entries must be origins" >&2; exit 1 ;;
        esac
    done
    cors_map "$origins"
    cors_headers="    add_header Access-Control-Allow-Origin \$aiva_cors_origin always;
    add_header Access-Control-Expose-Headers \"Retry-After, RateLimit, RateLimit-Policy, Idempotent-Replayed\" always;
    add_header Vary Origin always;"
    cors="    if (\$request_method = OPTIONS) {
        return 204;
    }
$cors_headers
    add_header Access-Control-Allow-Methods \"GET, POST\" always;
    add_header Access-Control-Allow-Headers \"X-AiVa-Authorization, Content-Type, Idempotency-Key\" always;
    add_header Access-Control-Max-Age 600 always;
    proxy_hide_header Access-Control-Allow-Origin;
    proxy_hide_header Access-Control-Allow-Credentials;
    proxy_hide_header Access-Control-Expose-Headers;
    proxy_hide_header Vary;"
fi

umask 077
cat > "$out" <<EOF
# Generated at container start by gateway-proxy.sh. Holds the proxy secret (and the HF token on the hop):
# never copy or log this file.
location /gateway/ {
    proxy_pass $upstream/;
    proxy_http_version 1.1;
$upstream_tls
    # The cold sovereign path answers 503 within about a second; live answers can take up to the gateway's
    # 120 s provider read timeout, so allow 180 s for the response and little for everything else.
    proxy_connect_timeout 10s;
    proxy_send_timeout 30s;
    proxy_read_timeout 180s;
    client_body_timeout 15s;
    client_max_body_size 2m;
    limit_req zone=aiva_gateway burst=20 nodelay;
    limit_conn aiva_gateway_conn 10;
    # The proxy's own limits answer the gateway-style JSON 429 (RATE_LIMITED). They use a status no upstream
    # sends, so the error_page below never touches an upstream 429: the gateway's own JSON, or HF's page
    # with its RateLimit headers, passes through unchanged.
    limit_req_status 469;
    limit_conn_status 469;
    error_page 469 = @aiva_proxy_rate_limited;

    proxy_pass_request_headers off;
    proxy_set_header Host "$host";
    proxy_set_header Connection "";
    proxy_set_header Content-Type \$content_type;
    proxy_set_header Content-Length \$content_length;
    proxy_set_header Accept \$http_accept;
    proxy_set_header Idempotency-Key \$http_idempotency_key;
    proxy_set_header X-AiVa-Authorization \$aiva_credential;
    proxy_set_header X-AiVa-Client-IP \$aiva_client_ip;
    proxy_set_header X-AiVa-Proxy-Secret "$proxy_secret";
$upstream_auth

    proxy_hide_header Set-Cookie;
    proxy_hide_header X-Proxied-Host;
    proxy_hide_header X-Proxied-Path;
    proxy_hide_header X-Proxied-Replica;
    proxy_hide_header Link;
    add_header X-Content-Type-Options "nosniff" always;
$cors

    # The gateway Space unreachable (sleeping, waking, restarting): 502/504 or a failed connect becomes a
    # retryable "starting" answer instead of a raw error. The gateway's own 503s (a cold sovereign endpoint,
    # a fail-closed dependency) carry meaningful JSON and pass through untouched.
    proxy_intercept_errors on;
    error_page 502 504 = @aiva_gateway_starting;
}

# The API description stays off the public front door (Govern, ADR-0025): the contract is in contracts/.
location ~ ^/gateway/(docs|redoc|openapi\.json)(/|$) {
    default_type application/json;
    add_header X-Content-Type-Options "nosniff" always;
    return 404 '{"error":{"code":"NOT_FOUND","message":"Not found.","retryable":false,"rule_ids":[],"run_id":null,"details":{}}}';
}

location @aiva_proxy_rate_limited {
$cors_headers
    add_header Retry-After 1 always;
    add_header Cache-Control "no-store" always;
    default_type application/json;
    return 429 '{"error":{"code":"RATE_LIMITED","message":"Too many requests from this browser at once.","remediation":"Retry in a moment.","retryable":true,"rule_ids":[],"run_id":null,"details":{"retry_after_seconds":1}}}';
}

location @aiva_gateway_starting {
$cors_headers
    add_header Retry-After 30 always;
    add_header Cache-Control "no-store" always;
    if (\$aiva_wants_html) {
        rewrite ^ /__aiva/gateway-starting.html last;
    }
    default_type application/json;
    return 503 '{"error":{"code":"GATEWAY_STARTING","message":"The demo backend is starting; ready in about 3 minutes.","remediation":"Retry in a minute.","retryable":true,"rule_ids":[],"run_id":null,"details":{}}}';
}

location = /__aiva/gateway-starting.html {
    internal;
    default_type text/html;
    add_header Retry-After 30 always;
    add_header Cache-Control "no-store" always;
    return 503 '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta http-equiv="refresh" content="20"><meta name="viewport" content="width=device-width, initial-scale=1"><title>AiVa is starting</title></head><body style="font-family:system-ui,sans-serif;margin:3rem auto;max-width:36rem;padding:0 1rem"><h1>The demo backend is starting</h1><p>It will be ready in about 3 minutes. This page retries on its own every 20 seconds.</p><p>All data in this demo is synthetic.</p></body></html>';
}
EOF
if [ "$loopback" = 1 ]; then
    echo "$0: /gateway/ is the front door to $upstream; CORS origins: ${origins:-none}"
else
    echo "$0: /gateway/ proxies to $upstream"
fi
