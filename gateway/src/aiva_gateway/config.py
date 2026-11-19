"""Runtime settings. Secrets come from the environment or the deployment's secret store only."""

from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]
# Environments where password-less dev sign-in is refused outright.
PRODUCTION_ENVIRONMENTS = frozenset({"prod", "production"})
# The only environments where password-less dev sign-in may be enabled (ADR-0010, ADR-0019): a developer's
# machine and the test suite. Every deployed gateway uses the passcode sign-in instead.
LOCAL_ENVIRONMENTS = frozenset({"local", "test"})
MIN_PROXY_SECRET_CHARS = 32
MIN_DEMO_PASSCODE_CHARS = 16
# Public demo sessions minted per hour (ADR-0019): per source and tenant-wide. Deployed gateways keep these;
# only the local and test environments may change them (an e2e suite or a replay gate needs more).
PUBLIC_SESSIONS_PER_SOURCE = 5
PUBLIC_SESSIONS_PER_TENANT = 200


class Settings(BaseSettings):
    # A validation error must never print the input: it holds every secret read from the environment.
    model_config = SettingsConfigDict(
        env_prefix="AIVA_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    environment: str = "local"
    # Connection URLs can carry a password (the gateway Space's Postgres and Redis URLs do), so they are
    # secrets: never shown in a repr, a dump or an error. Read them with `.get_secret_value()`.
    database_url: SecretStr = SecretStr("postgresql+asyncpg://aiva:aiva@127.0.0.1:5432/aiva")
    redis_url: SecretStr = SecretStr("redis://127.0.0.1:6379/0")
    opa_url: SecretStr = SecretStr("http://127.0.0.1:8181")
    opa_timeout_seconds: float = 2.0
    config_dir: Path = REPO_ROOT / "config"

    # Identity
    jwt_secret: SecretStr = SecretStr("local-dev-jwt-secret-change-me-0000000000")
    jwt_issuer: str = "aiva-local"
    jwt_audience: str = "aiva-gateway"
    credential_pepper: SecretStr = SecretStr("local-dev-credential-pepper")
    pseudonym_key: SecretStr = SecretStr("local-dev-pseudonym-key")

    # Ingress (ADR-0007). `direct`: the socket peer is the client, and X-Forwarded-For is walked past the
    # `trusted_proxies` CIDRs (empty = trust none). `platform-proxy` (HF Spaces, ADR-0019): requests arrive
    # through the platform's edge, so `Authorization` carries the platform's token and is never read; the
    # AiVa credential is `X-AiVa-Authorization`, and only a request carrying `proxy_secret` in
    # `X-AiVa-Proxy-Secret` is trusted for its attested client address.
    ingress: Literal["direct", "platform-proxy"] = "direct"
    proxy_secret: SecretStr | None = None
    trusted_proxies: list[str] = Field(default_factory=list)
    allow_simulated_ingress: bool = True
    # Password-less sign-in as a seeded demo user (`/v1/auth/dev/*`): it mints tokens for any demo user,
    # administrators included, so only the local and test environments allow it.
    dev_sign_in: bool = False
    # Passcode sign-in for the owner and demo users on deployed gateways (`/v1/auth/demo/*`, ADR-0019).
    # Unset = disabled.
    demo_passcode: SecretStr | None = None
    # Hourly public-session limits (ADR-0019); changeable only in the local and test environments.
    public_sessions_per_source: int = Field(default=PUBLIC_SESSIONS_PER_SOURCE, ge=1)
    public_sessions_per_tenant: int = Field(default=PUBLIC_SESSIONS_PER_TENANT, ge=1)
    # Browser origins allowed to call the API (the Control Center). Empty = no CORS.
    cors_origins: list[str] = Field(default_factory=list)

    # Govern (ADR-0011, ADR-0012). DLP runs Presidio with this spaCy English model; text beyond the scan
    # limit is refused (fail closed) rather than passed unscanned.
    dlp_nlp_model: str = "en_core_web_md"
    dlp_max_scan_chars: int = 250_000
    approval_ttl_seconds: int = 24 * 3600
    # Public demo (ADR-0019): live runs a public visitor may start in any rolling window.
    public_live_runs_per_window: int = 5
    public_live_window_seconds: int = 3600

    # FinOps (LLD §12). Open reservations older than this are settled from the ledger or released by
    # reconciliation; the job runs in the gateway every `reconcile_interval_seconds` (0 = off).
    reservation_stale_seconds: int = 900
    reconcile_interval_seconds: int = 300

    # Insights (LLD §21; ADR-0015). Aggregates are refreshed in the gateway every
    # `aggregate_interval_seconds` (0 = off); CSV exports expire after `report_export_ttl_seconds`.
    aggregate_interval_seconds: int = 60
    report_export_ttl_seconds: int = 3600

    # Execution modes (ADR-0009): "simulated" placeholder adapters (default); "live" provider calls;
    # "replay" recorded provider responses labelled `replayed`, never contacting a provider; "record" live
    # calls that also save recordings to `recordings_dir`.
    inference_mode: str = "simulated"
    data_designation: str = "synthetic"

    # --- Optimize & Inference (LLD §9-§11) ---------------------------------------------------------
    # OpenAI-compatible base URLs by model id, e.g. {"gct-qwen-finetuned": "https://…endpoints…"}.
    endpoint_urls: dict[str, str] = Field(default_factory=dict)
    # HF namespace whose Inference Endpoints are looked up by `endpoint_name` when no URL is configured.
    endpoint_namespace: str | None = None
    # Models kept on the simulated adapter in live mode (labelled `simulated`). Endpoints A and B are not
    # deployed yet (ML & Data); set AIVA_SIMULATED_MODELS='[]' once they are, so both call their endpoints.
    simulated_models: list[str] = Field(default_factory=lambda: ["gct-qwen-finetuned", "enterprise-cim"])
    recordings_dir: Path = REPO_ROOT / "gateway" / "tests" / "fixtures" / "recordings"
    provider_connect_timeout_seconds: float = 10.0
    provider_read_timeout_seconds: float = 120.0
    breaker_failure_threshold: int = 5
    breaker_reset_seconds: float = 30.0
    anthropic_effort: str = "medium"
    response_cache_enabled: bool = True
    semantic_cache_enabled: bool = True
    semantic_cache_threshold: float = 0.95
    retrieval_enabled: bool = True
    # "onnx" runs the pinned BAAI models in-process (CPU); "hashing"/"lexical" are dependency-free
    # stand-ins for tests and offline development.
    embedding_backend: str = "onnx"
    # The cross-encoder reranker is off by default ("none"); "onnx" enables the pinned bge-reranker-base. On
    # the 121 held-out RAG cases it did not improve recall and cost about 1.5 s per request (ADR-0009).
    reranker_backend: str = "none"
    # Heading-aware fusion for every request: a third ranking over section headings joins BM25 and vector
    # search (ADR-0009, 2026-10-18 amendment). Off: it is enabled per context profile, where it was measured
    # (`ContextProfile.heading_fusion`).
    retrieval_heading_fusion: bool = False
    # Endpoint A's output decode rate, for the CIM time-budget bypass (ML & Data's measurement, ADR-0009).
    cim_decode_tokens_per_s: float = 70.0
    # The same for a v2 CIM (ADR-0018). The 70 tok/s above is n-gram speculation on v1 replies that copy whole
    # passages; a v2 reply copies only ids and values. Measured (provenance `measured`) for v7@16bd341 on
    # Endpoint A's L4 image and args, per-stream p5 41, p50 62 tok/s; the p5, rounded down. Source: ML & Data,
    # 2026-10-18, ml/reports/endpoint-cim-v2-enterprise-cim-v7-bench.md.
    cim_v2_decode_tokens_per_s: float = 40.0
    # Runaway guard for a v2 CIM (ADR-0018): each call's guided-decoding schema narrows both id lists to the
    # ids sent (`enum`, `maxItems`). On Endpoint A's L4 bench (ML & Data, 2026-10-18) vLLM honours both, the
    # two invented-id loops stop, and 30 of 30 replies that did not loop are byte-identical.
    cim_v2_constrain_ids: bool = True
    # RLCD classifier (ADR-0009): "model" loads the pinned ModernBERT (needs the `rlcd` extra and HF_TOKEN);
    # "fixture" replays recorded model outputs from `rlcd_fixture_path` (tests); "heuristic" uses no model.
    rlcd_backend: str = "heuristic"
    rlcd_fixture_path: Path | None = None
    # A local copy of the pinned model directory, e.g. baked into the image; skips the Hub download.
    rlcd_local_dir: Path | None = None

    # Provider credentials (unused in simulated mode). Names match the deployment secrets.
    hf_token: SecretStr | None = Field(default=None, validation_alias="HF_TOKEN")
    # Cloud sessions withhold ANTHROPIC_API_KEY, so AIVA_ANTHROPIC_API_KEY is the primary name.
    anthropic_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("AIVA_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY")
    )
    gemini_api_key: SecretStr | None = Field(default=None, validation_alias="GEMINI_API_KEY")
    # The answer-quality grader's provider (ADR-0023), a Space 2 secret; never logged or printed.
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="AIVA_OPENAI_API_KEY")
    # Governed live web search (ADR-0022): the Tavily key, a Space 2 secret. Without it `web.search` is
    # unavailable and there is no fallback source.
    search_api_key: SecretStr | None = Field(default=None, validation_alias="AIVA_SEARCH_API_KEY")
    search_timeout_seconds: float = 10.0

    @property
    def reservation_stale(self) -> timedelta:
        return timedelta(seconds=self.reservation_stale_seconds)

    @property
    def dev_sign_in_allowed(self) -> bool:
        return self.dev_sign_in and self.environment.lower() in LOCAL_ENVIRONMENTS

    @property
    def public_session_limits(self) -> tuple[int, int]:
        """(per source, tenant-wide) public sessions per hour. Outside local and test always the defaults:
        `model_copy` skips the start-up validator, so the limiter never relies on it alone."""
        if self.environment.lower() in LOCAL_ENVIRONMENTS:
            return self.public_sessions_per_source, self.public_sessions_per_tenant
        return PUBLIC_SESSIONS_PER_SOURCE, PUBLIC_SESSIONS_PER_TENANT

    @model_validator(mode="after")
    def _public_session_limits_only_locally(self) -> Self:
        changed = (self.public_sessions_per_source, self.public_sessions_per_tenant) != (
            PUBLIC_SESSIONS_PER_SOURCE,
            PUBLIC_SESSIONS_PER_TENANT,
        )
        if changed and self.environment.lower() not in LOCAL_ENVIRONMENTS:
            raise ValueError(
                "AIVA_PUBLIC_SESSIONS_PER_SOURCE and AIVA_PUBLIC_SESSIONS_PER_TENANT may only be changed "
                f"when AIVA_ENVIRONMENT is local or test: the public demo keeps {PUBLIC_SESSIONS_PER_SOURCE} "
                f"per source and {PUBLIC_SESSIONS_PER_TENANT} tenant-wide per hour."
            )
        return self

    @model_validator(mode="after")
    def _dev_sign_in_only_locally(self) -> Self:
        if self.dev_sign_in and self.environment.lower() not in LOCAL_ENVIRONMENTS:
            raise ValueError(
                "AIVA_DEV_SIGN_IN may only be enabled when AIVA_ENVIRONMENT is local or test: it mints user "
                "tokens, administrators included, without a password. Use AIVA_DEMO_PASSCODE instead."
            )
        return self

    @model_validator(mode="after")
    def _ingress_secrets(self) -> Self:
        secret = self.proxy_secret.get_secret_value() if self.proxy_secret else ""
        if self.ingress == "platform-proxy" and len(secret) < MIN_PROXY_SECRET_CHARS:
            raise ValueError(
                f"AIVA_INGRESS=platform-proxy needs AIVA_PROXY_SECRET of at least {MIN_PROXY_SECRET_CHARS} "
                "characters, shared with the proxies."
            )
        passcode = self.demo_passcode.get_secret_value() if self.demo_passcode else None
        if passcode is not None and len(passcode) < MIN_DEMO_PASSCODE_CHARS:
            raise ValueError(f"AIVA_DEMO_PASSCODE must be at least {MIN_DEMO_PASSCODE_CHARS} characters.")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
