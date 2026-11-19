---
title: AiVa Gateway
emoji: 🛡️
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
startup_duration_timeout: 30m
short_description: AiVa gateway proof of concept (synthetic data)
---

# AiVa Gateway (gct-rlcd-gateway)

FastAPI gateway with the AI-PEL policy engine (OPA), RLCD routing, CIM orchestration, FinOps metering and
telemetry. All data is synthetic; GulfConnect Telecom is fictional and sovereignty on HF is simulated.

## What runs in the container

One container, because a Space exposes one port and has no sidecars. `supervisord` (as uid 1000) runs:

| Program | Listens on | Notes |
|---|---|---|
| Postgres 16 + pgvector | 127.0.0.1:5432 | Cluster in `$AIVA_DATA_DIR/postgres`; password generated per data directory |
| Redis 7 | 127.0.0.1:6379 | No persistence; password generated per data directory |
| OPA 1.20 | 127.0.0.1:8181 | Policy bundle pushed by the gateway |
| Front door (nginx) | 0.0.0.0:7860 | The only exposed port (ADR-0025). Serves the gateway under `/gateway/` only: sets the proxy proof and the visitor's address, applies the per-visitor limits and CORS for the two UIs. `/` is a one-line note; everything else, `/gateway/docs` and `/gateway/openapi.json` included, is 404 |
| Gateway (uvicorn) | 127.0.0.1:8000 | Loopback only: reached through the front door |
| Watchdog | — | Polls `/health/ready`; after an OPA restart this re-pushes the published policy bundle |

The Space is public and the UIs of Space 1 and Space 3 call `https://<owner>-gct-rlcd-gateway.hf.space/gateway`
from the browser, so no request crosses a Space-to-Space hop (ADR-0025). The front door starts with the data
services, so callers get the retryable `GATEWAY_STARTING` (503) while the gateway boots or restores.

Crashed programs restart. A program that keeps crashing turns FATAL, and the container stops so the Space
restarts it. While Postgres, Redis or OPA is down the gateway **fails closed** (LLD §17): requests are
refused, never served without policy or budget checks.

**Boot sequence** (`gateway.sh`, once per container start): wait for Postgres, Redis and OPA → create the
database → restore the newest backup, if any (`db_backup.py restore`) → `aiva-admin migrate` → `aiva-admin seed` (config/ into Postgres, policy bundle into OPA) →
`aiva-admin kb-ingest` of the synthetic knowledge base (`AIVA_BOOT_KB`, empty to skip) → fetch the RLCD model → optional synthetic traffic → uvicorn. A failed migrate, seed or KB ingest stops the container.

**The database survives restarts through a backup bucket (ADR-0024).** The Space disk is ephemeral, so every
restart, rebuild or sleep starts an empty Postgres cluster. With `AIVA_BACKUP_BUCKET` set, `db_backup.py`
handles this:
- On boot it restores the newest good dump from that private HF Storage Bucket into the empty database,
  before `migrate`. A restored database skips the synthetic boot traffic.
- While the gateway runs, the `backup` program checks right after boot and then every
  `AIVA_BACKUP_INTERVAL_SECONDS` (60). It uploads only when the data changed, and checks once more when the
  container stops. It keeps `AIVA_BACKUP_KEEP` rolling copies.
- Only a container whose boot restore restored a dump, found the bucket empty or found an existing database
  backs up. One that started fresh because the bucket was unreachable never replaces the bucket's dumps.

An unreachable bucket, or a missing or damaged dump, is logged, and the gateway starts on a fresh database
as before; it never blocks boot. Without the variable, the demo resets on every restart: runs, ledger,
audit log, approvals, policy drafts and Insights history are gone. See `deploy/RUNBOOK.md` §2 and §4.

## Models

- **RLCD** (`s-ttp/aiva-rlcd-modernbert`, pinned in `aiva_gateway.optimize.rlcd_model`): private, so it is
  **downloaded at boot** with `HF_TOKEN` into `$AIVA_DATA_DIR/models/` and loaded from there
  (`AIVA_RLCD_LOCAL_DIR`). It is not baked into the image, so the build needs no secret. Without a token,
  or if the download fails, the gateway runs the heuristic RLCD and records it as `heuristic-v0`.
- **Embedder and reranker** (`BAAI/bge-base-en-v1.5`, `BAAI/bge-reranker-base`, ONNX): public, **baked into
  the image** at their pinned revisions (`--build-arg AIVA_BAKE_ONNX=false` skips it; they then download
  on first use).

## Configuration

Image defaults (ADR-0009): `AIVA_ENVIRONMENT=demo`, `AIVA_INFERENCE_MODE=live`,
`AIVA_ENDPOINT_NAMESPACE=s-ttp`, `AIVA_SIMULATED_MODELS=[]`, `AIVA_RLCD_BACKEND=model`,
`AIVA_SEED_ON_START=true`, `AIVA_BOOT_TRAFFIC_RUNS=60`, `AIVA_DATA_DIR=/tmp/aiva-data`,
`AIVA_RECORDINGS_DIR=/app/deploy/recordings`, `AIVA_INGRESS=platform-proxy` (the image only runs behind its
own front door, so a missing Space variable cannot silently turn the proxy proof off).

| Name | Kind | Purpose |
|---|---|---|
| `HF_TOKEN` | secret | RLCD download and Endpoints A/B (namespace `s-ttp`) |
| `AIVA_ANTHROPIC_API_KEY`, `GEMINI_API_KEY` | secret | Claude and Gemini routes; DeepSeek uses `HF_TOKEN` via HF Inference Providers |
| `AIVA_JWT_SECRET` | secret | Signs user JWTs; the Context Lab Space must hold the same value |
| `AIVA_CREDENTIAL_PEPPER`, `AIVA_PSEUDONYM_KEY` | secret | Credential hashing and pseudonyms |
| `AIVA_INGRESS` | variable | `platform-proxy` on the Space: requests arrive through the front door; `Authorization` is never read, the user token is `X-AiVa-Authorization` (ADR-0019, ADR-0025) |
| `AIVA_PROXY_SECRET` | secret | Shared by the front door and the gateway in this container (at least 32 characters); without it the gateway refuses to start in platform-proxy mode, and the front door answers 503 |
| `AIVA_PROXY_CORS_ORIGINS` | variable | The browser origins the front door answers CORS for, space-separated: the Control Center's and the Context Lab's (ADR-0025) |
| `AIVA_DEMO_PASSCODE` | secret | Passcode sign-in for the owner and demo users (at least 16 characters, ADR-0019) |
| `AIVA_SEARCH_API_KEY` | secret | Tavily key for the market-research use case's governed `web.search` (ADR-0022). Without it `web.search` is unavailable |
| `AIVA_BACKUP_BUCKET` | variable | The private HF Storage Bucket for database backups, `s-ttp/aiva-gateway-backups` (ADR-0024). Unset: no backup or restore |
| `AIVA_BACKUP_HF_TOKEN` | secret | Fine-grained token with read and write access to that bucket only. Without it, or with the bucket unreachable, boot continues on a fresh database |
| `AIVA_BACKUP_INTERVAL_SECONDS`, `AIVA_BACKUP_KEEP` | variable | Check every 60 s and upload when the data changed; keep 3 dumps (defaults) |
| `AIVA_OPENAI_API_KEY` | secret | OpenAI key for the presenter's answer-quality grader (ADR-0023). Without it the comparison reports `failed`; the run itself is unaffected |
| `AIVA_CORS_ORIGINS` | variable | `[]` on the Space: the front door answers CORS (`AIVA_PROXY_CORS_ORIGINS`), so the gateway adds none |
| `AIVA_BOOT_TRAFFIC_RUNS` | variable | Synthetic Insights runs generated into a fresh database (default `60`, `0` disables); simulated inference, labelled synthetic, no spend. The dashboards therefore start with synthetic history |
| `AIVA_DATA_DIR` | variable | Postgres data, local passwords and the RLCD model (default `/tmp/aiva-data`) |
| `AIVA_RECORDINGS_DIR` | variable | Recorded exchanges for replay runs (ADR-0019): the scenario recordings shipped in `deploy/spaces/gateway/recordings/`. Without any, every replay is a miss answered `simulated`. For the recording run only: a writable directory such as `/tmp/aiva-data/recordings` (DEPLOY_PLAN step 5) |
| `AIVA_DEV_SIGN_IN` | variable | `false` on the Space; see below |

`AIVA_DATABASE_URL`, `AIVA_REDIS_URL` and `AIVA_OPA_URL` are set by the entrypoint for the local services;
set them only to use external services instead.

**Password-less dev sign-in is local and test only.** It mints user tokens, administrators included,
without a password, so the gateway refuses to start with `AIVA_DEV_SIGN_IN=true` unless `AIVA_ENVIRONMENT`
is `local` or `test` (ADR-0010, ADR-0019). Deployed gateways sign in with `AIVA_DEMO_PASSCODE`.

**Public-session limits are fixed on the Space.** `AIVA_PUBLIC_SESSIONS_PER_SOURCE` and
`AIVA_PUBLIC_SESSIONS_PER_TENANT` (5 and 200 per hour) may be changed only in `local` and `test`, for e2e
suites and replay gates; a deployed gateway with either set refuses to start (ADR-0019).

## Hardware

Recommended: **CPU Upgrade** (8 vCPU, 32 GB, $0.03/h on hf.co/pricing, 2026-10-15). The RLCD model alone
takes about 1.6–2.0 GB RSS (ADR-0009), plus the ONNX models, spaCy, Postgres and Redis; RLCD p95 was 398 ms
on 4 vCPU (measured, ADR-0009) against a 2 s budget. The free CPU Basic (2 vCPU, 16 GB) fits in memory but
leaves little CPU headroom for RLCD, DLP and the ONNX models.

Measured locally on 4 vCPU (2026-10-15, this image, simulated inference, ModernBERT RLCD):
- Boot to ready takes **121 s**. That covers migrate, seed, KB ingest, the RLCD download and 60
  synthetic runs.
- The container uses **3.9 GiB** after warm-up, with RLCD, ONNX, spaCy, Postgres and Redis loaded.
- See `deploy/benchmarks/` for latency under load.

## Build and run locally

```sh
docker build -f deploy/spaces/gateway/Dockerfile -t aiva-gateway .
docker run --rm -p 8000:7860 -e AIVA_ENVIRONMENT=local -e AIVA_INFERENCE_MODE=simulated \
  -e AIVA_RLCD_BACKEND=heuristic -e AIVA_DEV_SIGN_IN=true -e AIVA_BOOT_TRAFFIC_RUNS=60 \
  -e AIVA_PROXY_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" \
  -e AIVA_PROXY_CORS_ORIGINS="http://localhost:5173" aiva-gateway
# API at http://localhost:8000/gateway/ (the front door needs a proxy secret, any 32+ characters locally)
bash deploy/spaces/gateway/smoke_test.sh aiva-gateway   # what CI runs
```
