#!/usr/bin/env bash
# Smoke test for the server-side /gateway/ proxy in the Control Center (Space 1) and Context Lab (Space 3)
# images, against a stub upstream that reports what it received:
#   bash deploy/spaces/shared/proxy_smoke_test.sh [control-center image] [context-lab image]
# Checks (Govern's ingress spec): the browser's Authorization arrives as X-AiVa-Authorization, and none
# is invented when the browser sent none; a client-sent X-AiVa-Authorization takes precedence; a client's
# HF token (`Bearer hf_…`) is never forwarded; upstream Authorization is the Space's HF token; the proxy secret
# is set; X-AiVa-Client-IP is the rightmost X-Forwarded-For entry; client-sent X-AiVa-*, X-Forwarded-*,
# X-Real-IP and Cookie never reach upstream; upstream Set-Cookie never reaches the browser; the HF token
# never appears in anything the browser receives; a burst is rate-limited (429); the log holds no token,
# secret or auth header and records HF's RateLimit headers on its 429, which passes through unchanged; the proxy's
# own 429 is the JSON envelope (RATE_LIMITED, Retry-After); without the proxy secret the proxy answers 503. All
# values are fake.
set -euo pipefail

CC_IMAGE="${1:-aiva-control-center}"
LAB_IMAGE="${2:-aiva-context-lab}"
STUB_IMAGE="${STUB_IMAGE:-python:3.12.13-slim-bookworm}"
NET="aiva-proxy-smoke-$$"
FAKE_TOKEN="hf_proxysmoketestNotARealToken0000"
FAKE_PROXY_SECRET="proxy-smoke-test-secret-not-real-000000"
JWT="eyJhbGciOiJIUzI1NiJ9.proxy-smoke.test-signature"
names=()

cleanup() {
    for n in "${names[@]}"; do docker rm -f "$n" >/dev/null 2>&1 || true; done
    docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT
fail() { echo "PROXY SMOKE FAILED: $*" >&2; exit 1; }

# The stub reports header names, the path, the body length and whether the upstream Authorization equals
# the fake token. The only header value it echoes is X-AiVa-Authorization (the test JWT).
read -r -d '' STUB <<'PY' || true
import json, os
from http.server import BaseHTTPRequestHandler, HTTPServer

class Echo(BaseHTTPRequestHandler):
    def _reply(self):
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        out = json.dumps({
            "path": self.path,
            "method": self.command,
            "names": sorted({k.lower() for k in self.headers.keys()}),
            "authorization_is_token": self.headers.get("authorization") == "Bearer " + os.environ["FAKE_TOKEN"],
            "proxy_secret_ok": self.headers.get("x-aiva-proxy-secret") == os.environ["FAKE_PROXY_SECRET"],
            "x_aiva_authorization": self.headers.get("x-aiva-authorization"),
            "x_aiva_client_ip": self.headers.get("x-aiva-client-ip"),
            "body_length": len(body),
        }).encode()
        if self.path.startswith("/v1/hf-429"):  # HF's edge rate-limiting the hop: its HTML page and quota headers
            out = b"<html><body>We had to rate limit you.</body></html>"
            self.send_response(429)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("RateLimit", '"pages";r=0;t=212')
            self.send_header("RateLimit-Policy", '"fixed window";"pages";q=400;w=300')
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        if self.path.startswith("/v1/own-503"):  # the gateway's own retryable 503 (e.g. a cold sovereign path)
            out = json.dumps({"error": {"code": "SOVEREIGN_ENDPOINT_UNAVAILABLE"}}).encode()
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", "upstream=1; Path=/")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)
    do_GET = do_POST = _reply
    def log_message(self, *a): pass

HTTPServer(("0.0.0.0", 9000), Echo).serve_forever()
PY

docker network create "$NET" >/dev/null
names+=("$NET-stub")
docker run -d --name "$NET-stub" --network "$NET" --network-alias stub -e FAKE_TOKEN="$FAKE_TOKEN" \
    -e FAKE_PROXY_SECRET="$FAKE_PROXY_SECRET" \
    "$STUB_IMAGE" python -c "$STUB" >/dev/null

check_image() { # label image port extra-env...
    local label="$1" image="$2" port="$3"
    shift 3
    local name="$NET-$label"
    names+=("$name" "$name-notoken")
    docker run -d --name "$name" --network "$NET" -p "127.0.0.1:$port:7860" \
        -e AIVA_GATEWAY_UPSTREAM=http://stub:9000 -e AIVA_PROXY_ALLOW_HTTP_UPSTREAM=1 \
        -e HF_TOKEN="$FAKE_TOKEN" -e AIVA_PROXY_SECRET="$FAKE_PROXY_SECRET" \
        -e AIVA_GATEWAY_URL="http://127.0.0.1:$port/gateway" "$@" "$image" >/dev/null
    local base="http://127.0.0.1:$port"
    for _ in $(seq 1 60); do # the UI is up and the stub upstream answers through the proxy
        curl -s "$base/gateway/v1/ready" | grep -q '"names"' && break
        sleep 1
    done

    local headers body
    headers="$(mktemp)"
    body="$(curl -sS -D "$headers" -X POST "$base/gateway/v1/echo?probe=1" \
        -H "Authorization: Bearer $JWT" -H "Content-Type: application/json" \
        -H "X-Forwarded-For: 198.51.100.9, 203.0.113.66" -H "X-Forwarded-Host: evil.example" \
        -H "X-Real-IP: 198.51.100.9" \
        -H "X-AiVa-Proxy-Secret: forged" -H "X-AiVa-Client-IP: 198.51.100.9" -H "Cookie: hf-session=x" \
        --data '{"probe":true}')" || fail "$label: request failed"

    python3 - "$label" "$JWT" "$body" <<'PY' || fail "$label: upstream saw the wrong request"
import json, sys
label, jwt, body = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
names = set(body["names"])
assert body["path"] == "/v1/echo?probe=1", body["path"]
assert body["x_aiva_authorization"] == "Bearer " + jwt, "Authorization not moved to X-AiVa-Authorization"
assert body["authorization_is_token"], "upstream Authorization is not the Space token"
assert body["proxy_secret_ok"], "X-AiVa-Proxy-Secret is not the Space's proxy secret"
# The rightmost X-Forwarded-For entry is the hop HF's edge appends (emulated here by the client).
assert body["x_aiva_client_ip"] == "203.0.113.66", body["x_aiva_client_ip"]
assert body["body_length"] == len('{"probe":true}'), "request body lost"
leaked = names & {"x-forwarded-for", "x-forwarded-host", "cookie", "x-real-ip"}
assert not leaked, f"client headers reached upstream: {sorted(leaked)}"
print(f"ok: {label}: upstream got {sorted(names)}")
PY
    grep -qi '^set-cookie' "$headers" && fail "$label: upstream Set-Cookie reached the browser"
    { cat "$headers"; echo "$body"; curl -s "$base/config.js"; } | grep -q "$FAKE_TOKEN" \
        && fail "$label: the HF token reached the browser"
    rm -f "$headers"
    echo "ok: $label: header move, proxy secret, client IP, stripping, no Set-Cookie, no token to the browser"

    # No Authorization from the browser: no X-AiVa-Authorization upstream (the public visitor).
    curl -sS "$base/gateway/v1/echo" | python3 -c '
import json, sys
body = json.load(sys.stdin)
assert body["x_aiva_authorization"] is None, "the proxy invented an X-AiVa-Authorization"
assert "x-aiva-authorization" not in body["names"]
' || fail "$label: X-AiVa-Authorization sent without a browser Authorization"
    echo "ok: $label: no browser Authorization -> no X-AiVa-Authorization"

    # A scripted client on a private Space: its HF token in Authorization (HF's edge), the AiVa token in
    # X-AiVa-Authorization. The AiVa token is forwarded; the client's HF token never is.
    local client_hf="hf_clientPlatformTokenNotReal0000"
    curl -sS "$base/gateway/v1/echo" -H "Authorization: Bearer $client_hf" \
        -H "X-AiVa-Authorization: Bearer $JWT" | python3 -c '
import json, sys
body = json.load(sys.stdin)
assert body["x_aiva_authorization"] == "Bearer " + sys.argv[1], "client X-AiVa-Authorization not forwarded"
assert body["authorization_is_token"], "upstream Authorization is not the Space token"
' "$JWT" || fail "$label: client X-AiVa-Authorization not preferred"
    for presented in "Authorization: bearer $client_hf" "Authorization: $client_hf" \
        "X-AiVa-Authorization: Bearer $client_hf" "X-AiVa-Authorization: $client_hf"; do
        curl -sS "$base/gateway/v1/echo" -H "$presented" | python3 -c '
import json, sys
body = json.load(sys.stdin)
assert body["x_aiva_authorization"] is None, "a client HF token was forwarded as X-AiVa-Authorization"
' || fail "$label: a client HF token was forwarded"
    done
    echo "ok: $label: client X-AiVa-Authorization preferred; a client HF token is never forwarded"

    # Per-visitor rate limit on /gateway/ (5 r/s, burst 20): a burst of 60 must see 429s.
    local limited
    limited="$(for _ in $(seq 1 60); do
        curl -s -o /dev/null -w '%{http_code}\n' -H "X-Forwarded-For: 192.0.2.77" "$base/gateway/v1/echo" &
    done | sort | uniq -c)"
    wait
    grep -q " 429$" <<<"$limited" || fail "$label: no 429 under a burst of 60 ($limited)"
    # The proxy's own 429 is the gateway-style JSON envelope with Retry-After, never nginx's HTML page.
    local own_429
    own_429="$(for _ in $(seq 1 40); do
        curl -s -D - -H "X-Forwarded-For: 192.0.2.79" "$base/gateway/v1/echo" &
    done)"
    wait
    grep -q '"code":"RATE_LIMITED"' <<<"$own_429" || fail "$label: the proxy's 429 is not the JSON envelope"
    grep -qi '^retry-after: 1' <<<"$own_429" || fail "$label: the proxy's 429 has no Retry-After"
    grep -qi '<html' <<<"$own_429" && fail "$label: the proxy answered a 429 with an HTML page"
    echo "ok: $label: rate limit, as the JSON envelope with Retry-After ($(tr '\n' ' ' <<<"$limited"))"

    # HF's own 429 reaches the client unchanged (its page and quota headers), and the access log records them.
    local hf_429
    hf_429="$(curl -sS -D - -H "X-Forwarded-For: 192.0.2.78" "$base/gateway/v1/hf-429")"
    grep -qi '^ratelimit-policy: "fixed window";"pages";q=400;w=300' <<<"$hf_429" \
        || fail "$label: HF's RateLimit-Policy did not reach the client"
    grep -qF 'We had to rate limit you' <<<"$hf_429" || fail "$label: HF's 429 page was replaced by the proxy"

    # Nothing secret in the container log (access log format, startup messages).
    local logs
    logs="$(docker logs "$name" 2>&1)"
    for secret in "$FAKE_TOKEN" "$FAKE_PROXY_SECRET" "$JWT" "$client_hf" forged; do
        grep -qF -- "$secret" <<<"$logs" && fail "$label: a secret or auth header value is in the log"
    done
    grep -qF 'GET /gateway/v1/hf-429" 429' <<<"$logs" \
        && grep -F '/v1/hf-429' <<<"$logs" | grep -qF 'rl="\x22pages\x22;r=0;t=212"' \
        || fail "$label: the access log lacks HF's RateLimit headers on its 429"
    echo "ok: $label: log holds no token, proxy secret or auth header; HF's 429 logs its quota"

    # The gateway's own 503 passes through untouched (not replaced by the "starting" answer).
    curl -s "$base/gateway/v1/own-503" | grep -q SOVEREIGN_ENDPOINT_UNAVAILABLE \
        || fail "$label: the gateway's own 503 was replaced"
    echo "ok: $label: gateway 503 passes through"

    # Gateway Space unreachable (closed port: connect refused -> 502): a retryable "starting" answer.
    names+=("$name-down")
    docker run -d --name "$name-down" --network "$NET" -p "127.0.0.1:$((port + 2)):7860" \
        -e AIVA_GATEWAY_UPSTREAM=http://stub:9999 -e AIVA_PROXY_ALLOW_HTTP_UPSTREAM=1 \
        -e HF_TOKEN="$FAKE_TOKEN" -e AIVA_PROXY_SECRET="$FAKE_PROXY_SECRET" "$@" "$image" >/dev/null
    local down="http://127.0.0.1:$((port + 2))" json=""
    for _ in $(seq 1 60); do
        json="$(curl -s -H "Accept: application/json" "$down/gateway/v1/auth/me" || true)"
        grep -q GATEWAY_STARTING <<<"$json" && break
        sleep 1
    done
    grep -q '"retryable":true' <<<"$json" || fail "$label: no retryable GATEWAY_STARTING JSON ($json)"
    [ "$(curl -s -o /dev/null -w '%{http_code}' "$down/gateway/v1/auth/me")" = 503 ] \
        || fail "$label: the starting answer is not a 503"
    curl -s -H "Accept: text/html" "$down/gateway/" | grep -q "The demo backend is starting" \
        || fail "$label: no HTML starting page for a navigation"
    echo "ok: $label: unreachable gateway -> GATEWAY_STARTING (JSON for API calls, HTML for navigations)"

    # Without the proxy secret (or the token) the proxy refuses (503) and the stub is never called.
    docker run -d --name "$name-notoken" --network "$NET" -p "127.0.0.1:$((port + 1)):7860" \
        -e AIVA_GATEWAY_UPSTREAM=http://stub:9000 -e AIVA_PROXY_ALLOW_HTTP_UPSTREAM=1 \
        -e HF_TOKEN="$FAKE_TOKEN" "$@" "$image" >/dev/null
    local code=""
    for _ in $(seq 1 60); do
        code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$((port + 1))/gateway/v1/x" || true)"
        [ "$code" = 503 ] && break
        sleep 1
    done
    [ "$code" = 503 ] || fail "$label: without AIVA_PROXY_SECRET the proxy returned $code, not 503"
    echo "ok: $label: no proxy secret -> 503"
}

check_image control-center "$CC_IMAGE" 17870
check_image context-lab "$LAB_IMAGE" 17880 -e AIVA_LAB_REGISTRY=fixtures
echo "proxy smoke test passed"
