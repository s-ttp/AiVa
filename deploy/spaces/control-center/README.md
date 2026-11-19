---
title: AiVa Control Center
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
short_description: AiVa Control Center proof of concept (synthetic data)
---

# AiVa Control Center

Hugging Face Docker Space for the AiVa Control Center: the AiVa Test Console and AiVa
Admin Dashboard (LLD v1.2 §4, §14, §22). Technical name: `gct-ai-control-center`.

All data is synthetic. GulfConnect Telecom (GCT) is a fictional reference tenant, and AiVa
is a working brand pending trademark clearance.

## What the image does

- **Build stage** (`node:22.23.3-trixie-slim`, pnpm 10.34.6): installs `web/` from the frozen
  lockfile and builds `@aiva/control-center`.
- **Runtime** (`nginxinc/nginx-unprivileged:1.30.4-alpine3.24`): serves the static build on
  port 7860 as uid 1000. Built files are root-owned and read-only; deep links fall back to
  `index.html`; content-hashed assets are cached immutably.
- **Runtime configuration**: at container start,
  `/docker-entrypoint.d/40-aiva-runtime-config.sh` writes `/config.js` (served with
  `Cache-Control: no-store`) from `AIVA_GATEWAY_URL` and `AIVA_STORY_MODE`, so one image serves every
  environment.

## Configuration

This Space is **public**; the gateway Space is private. The browser talks to this origin only: nginx
proxies `/gateway/` server-side to the gateway Space (`deploy/spaces/shared/gateway-proxy.sh`), moving the
browser's `Authorization` (the AiVa user token) to `X-AiVa-Authorization` and authenticating to the private
Space with this Space's `HF_TOKEN`. Only a whitelist of request headers is forwarded (Govern's ingress spec, ADR-0019): the proxy adds
`X-AiVa-Proxy-Secret` and `X-AiVa-Client-IP` (the rightmost `X-Forwarded-For` hop, which HF's edge appends);
client-sent `X-AiVa-*`, `X-Forwarded-*`, `X-Real-IP` and cookies never reach the gateway, and upstream
`Set-Cookie` never reaches the browser. `/gateway/` is rate-limited per visitor (5 requests/s, burst 20) and
the access log records no headers.

| Variable | Set as | Purpose |
| --- | --- | --- |
| `AIVA_GATEWAY_URL` | Space variable | `https://<owner>-gct-ai-control-center.hf.space/gateway` (this Space's own origin + `/gateway`); written into `/config.js` |
| `AIVA_STORY_MODE` | Space variable | `true` opens the story-led visitor screens (story home, Watch a request, This week) in front of the technical view; unset or `false` keeps them off. Written into `/config.js` |
| `AIVA_GATEWAY_UPSTREAM` | Space variable | `https://<owner>-gct-rlcd-gateway.hf.space`, the private gateway Space (an `https://` origin) |
| `HF_TOKEN` | Space **secret** | Fine-grained token with read access to the gateway Space only. Without it `/gateway/` answers 503 |
| `AIVA_PROXY_SECRET` | Space **secret** | Shared with the gateway (and Space 3): sent as `X-AiVa-Proxy-Secret` on every proxied request. Without it `/gateway/` answers 503 |

- `AIVA_GATEWAY_URL` must start with `http://` or `https://` and contain only `A-Z a-z 0-9 . _ ~ : / % -`;
  anything else stops the container at start-up. Unset: the UI reports the gateway as "Not configured".
- `AIVA_STORY_MODE` must be `true` or `false`; anything else stops the container at start-up. Any tab can preview
  story mode with `?view=story` (and leave it with `?view=classic`) whatever the variable says.
- The token lives only in the running container (`/var/run/aiva/gateway-proxy.conf`), never in the image
  or the repository. A public Space's repository files are public: `deploy/scripts/push_spaces.py` refuses
  to push a tree with env or key files or token-shaped strings.
- Never put provider API keys on this Space; only the gateway holds them (LLD §5).

## Build and run locally

From the repository root:

```sh
docker build -f deploy/spaces/control-center/Dockerfile -t aiva-control-center .
docker run --rm -p 7860:7860 -e AIVA_GATEWAY_URL=http://localhost:8000 aiva-control-center
```

Then open <http://localhost:7860>.

## Publishing to Hugging Face

`deploy/push_spaces.sh` stages this Space's repository (Dockerfile and README at the root,
`Dockerfile.dockerignore` as `.dockerignore`, and `web/` plus the deploy files, without tests), checks it
for secrets and, with `--apply` after the deploy plan is approved, pushes it. See `deploy/RUNBOOK.md`.

## Not yet included

- No Content-Security-Policy header yet; with the same-origin proxy, `connect-src 'self'` is now possible.
- No `X-Frame-Options`, so the Space can render inside the huggingface.co page.
