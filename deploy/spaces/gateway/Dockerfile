# syntax=docker/dockerfile:1
# HF Docker Space 2: gct-rlcd-gateway. Build context: repository root.
#   docker build -f deploy/spaces/gateway/Dockerfile -t aiva-gateway .
#
# One container runs Postgres 16 + pgvector, Redis 7, OPA, the gateway and its nginx front door under
# supervisord, because a Space exposes one port and has no sidecars. nginx owns that port (ADR-0025); the
# gateway and the data services listen on 127.0.0.1 only, the data services with generated passwords. The
# database is restored from the backup bucket on boot (ADR-0024, deploy/RUNBOOK.md).
FROM python:3.12.13-slim-bookworm

ARG OPA_VERSION=1.20.0
ARG PG_MAJOR=16
# Bake the public BAAI ONNX models (retrieval and semantic cache, ~1.5 GB) so a restart does not
# download them again. The private RLCD model is fetched at boot with HF_TOKEN instead (no build secret).
ARG AIVA_BAKE_ONNX=true

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH=/app/.venv/bin:/usr/lib/postgresql/${PG_MAJOR}/bin:$PATH \
    HF_HOME=/app/hf-home \
    HF_HUB_DISABLE_TELEMETRY=1 \
    AIVA_CONFIG_DIR=/app/config

# Postgres from the PGDG repository (bookworm ships 15); no cluster is created at install time.
RUN apt-get update -q \
 && apt-get install -y -q --no-install-recommends ca-certificates curl gnupg \
 && install -d /usr/share/postgresql-common/pgdg /etc/postgresql-common \
 && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
      https://www.postgresql.org/media/keys/ACCC4CF8.asc \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
 && echo "create_main_cluster = false" > /etc/postgresql-common/createcluster.conf \
 && apt-get update -q \
 && apt-get install -y -q --no-install-recommends \
      postgresql-${PG_MAJOR} postgresql-${PG_MAJOR}-pgvector redis-server supervisor nginx \
 && apt-get purge -y gnupg && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
ADD --chmod=755 https://openpolicyagent.org/downloads/v${OPA_VERSION}/opa_linux_amd64_static /usr/local/bin/opa

# HF Spaces run the container as uid 1000.
RUN useradd --create-home --uid 1000 user
WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY gateway/pyproject.toml gateway/pyproject.toml
# The `rlcd` extra adds CPU PyTorch and transformers for the ModernBERT RLCD (ADR-0009).
RUN uv sync --frozen --no-dev --package aiva-gateway --extra rlcd --no-install-workspace

COPY gateway/ gateway/
RUN uv sync --frozen --no-dev --package aiva-gateway --extra rlcd

COPY deploy/spaces/gateway/prefetch_models.py /app/deploy/prefetch_models.py
# Same layer as the download, so the chown does not copy 1.5 GB into a second layer.
RUN mkdir -p /app/hf-home \
 && if [ "$AIVA_BAKE_ONNX" = "true" ]; then python /app/deploy/prefetch_models.py onnx; fi \
 && chown -R user:user /app/hf-home

COPY config/ config/
COPY policies/ policies/
# Synthetic retrieval knowledge base (ML & Data export), ingested at every boot.
COPY data/synthetic/kb/gct_kb.jsonl /app/data/kb/gct_kb.jsonl
COPY deploy/spaces/gateway/ /app/deploy/
COPY deploy/spaces/shared/gateway-proxy.sh deploy/spaces/shared/aiva-http.conf /app/deploy/
RUN chmod 755 /app/deploy/*.sh \
 && mkdir -p /data && chown user:user /data

# Deployed defaults (ADR-0009): live providers, Endpoints A/B in s-ttp, ModernBERT RLCD. Space variables
# override them; compose.yaml and CI switch inference back to simulated. Per-run replay (ADR-0019) reads the
# scenario recordings shipped in deploy/spaces/gateway/recordings/; without them a replay is a simulated miss.
# The gateway sits behind its own front door on the loopback (ADR-0025), so platform-proxy ingress is baked in:
# without it the proof check would be off and every caller would look like 127.0.0.1.
ENV AIVA_ENVIRONMENT=demo \
    AIVA_INFERENCE_MODE=live \
    AIVA_ENDPOINT_NAMESPACE=s-ttp \
    AIVA_SIMULATED_MODELS=[] \
    AIVA_RLCD_BACKEND=model \
    AIVA_SEED_ON_START=true \
    AIVA_BOOT_TRAFFIC_RUNS=60 \
    AIVA_BOOT_KB=/app/data/kb/gct_kb.jsonl \
    AIVA_DATA_DIR=/tmp/aiva-data \
    AIVA_RECORDINGS_DIR=/app/deploy/recordings \
    AIVA_GATEWAY_PORT=8000 \
    AIVA_GATEWAY_UPSTREAM=http://127.0.0.1:8000 \
    AIVA_INGRESS=platform-proxy

USER user
EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=600s \
    CMD curl -sf http://127.0.0.1:7860/gateway/health/live || exit 1
ENTRYPOINT ["/app/deploy/entrypoint.sh"]
