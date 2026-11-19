"""Service container: wires each stage interface to its implementation (ADR-0004).

Workstreams swap a placeholder by changing its constructor here; tests pass overrides to `build_services`.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from aiva_gateway.assurance.checks import BaselineAssurance
from aiva_gateway.auth.ingress import Network, parse_networks
from aiva_gateway.catalog import Catalog, load_catalog
from aiva_gateway.config import Settings
from aiva_gateway.db.session import create_engine, create_sessionmaker
from aiva_gateway.finops.pricing import PriceBook
from aiva_gateway.finops.quota import RedisQuotaService
from aiva_gateway.govern.policy_engine import OpaPolicyEngine
from aiva_gateway.govern.presidio_dlp import PresidioDlpScanner, catalog_allow_terms
from aiva_gateway.govern.vocabulary import load_tenant_vocabulary
from aiva_gateway.inference.endpoints import EndpointResolver
from aiva_gateway.inference.factory import (
    build_adapter_registry,
    build_replay_registry,
    build_replay_resolver,
    build_resolver,
    validate_mode,
)
from aiva_gateway.inference.registry import AdapterRegistry
from aiva_gateway.optimize.context_guard import DeterministicContextGuard
from aiva_gateway.optimize.factory import (
    build_context_intelligence,
    build_replay_context_intelligence,
    build_response_cache,
    build_retriever,
    build_shared_embedder,
)
from aiva_gateway.optimize.rlcd import ModelRouter
from aiva_gateway.optimize.rlcd_model import build_rlcd
from aiva_gateway.optimize.web_search import WebEvidenceMemory, WebSearch, build_web_search
from aiva_gateway.pipeline.interfaces import (
    AssuranceService,
    ContextGuard,
    ContextIntelligence,
    DlpScanner,
    PolicyEngine,
    QuotaService,
    ResponseCache,
    Retriever,
    RlcdClassifier,
    Router,
)


@dataclass
class Services:
    settings: Settings
    catalog: Catalog
    prices: PriceBook
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    redis: Redis
    opa: OpaPolicyEngine
    policy_engine: PolicyEngine
    dlp: DlpScanner
    cache: ResponseCache
    router: Router
    rlcd: RlcdClassifier
    retriever: Retriever
    cim: ContextIntelligence
    context_guard: ContextGuard
    quota: QuotaService
    adapters: AdapterRegistry
    # Per-run replay (ADR-0019): recorded exchanges only, a miss answered by the simulated adapter.
    replay_adapters: AdapterRegistry
    replay_cim: ContextIntelligence
    # The configured set's endpoint resolver: a visitor's live run reads endpoint state through it.
    resolver: EndpointResolver
    assurance: AssuranceService
    # Governed live web search (ADR-0022): unavailable without `AIVA_SEARCH_API_KEY` or on a gateway that
    # does not run live inference.
    search: WebSearch
    trusted_proxies: list[Network] = field(default_factory=list)
    closers: list[Callable[[], Awaitable[None]]] = field(default_factory=list)
    # Screened web passages of recent runs, in memory only, for a presenter's comparison (ADR-0023).
    web_evidence: WebEvidenceMemory = field(default_factory=WebEvidenceMemory)

    async def aclose(self) -> None:
        await self.adapters.aclose()
        await self.replay_adapters.aclose()
        for close in self.closers:
            await close()
        await self.opa.aclose()
        await self.redis.aclose()
        await self.engine.dispose()


def build_services(settings: Settings, **overrides: Any) -> Services:
    mode = validate_mode(settings)
    catalog = load_catalog(settings.config_dir)
    engine = create_engine(settings.database_url.get_secret_value())
    sessionmaker = create_sessionmaker(engine)
    redis = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
    opa = OpaPolicyEngine(settings.opa_url.get_secret_value(), settings.opa_timeout_seconds)
    dlp = PresidioDlpScanner(
        model=settings.dlp_nlp_model,
        allow_terms=catalog_allow_terms(catalog),
        vocabulary=load_tenant_vocabulary(settings.config_dir),
        max_chars=settings.dlp_max_scan_chars,
    )
    prices = PriceBook(catalog)
    endpoints_http = httpx.AsyncClient(timeout=10.0) if mode in ("live", "record") else None
    resolver = build_resolver(settings, endpoints_http)
    embedder = build_shared_embedder(settings)
    cim = build_context_intelligence(settings, catalog, resolver)
    replay_resolver = build_replay_resolver(settings)
    replay_cim = build_replay_context_intelligence(settings, catalog, replay_resolver)
    closers: list[Callable[[], Awaitable[None]]] = []
    if endpoints_http is not None:
        closers.append(endpoints_http.aclose)
    if (close_cim := getattr(cim, "aclose", None)) is not None:
        closers.append(close_cim)
    if (close_replay_cim := getattr(replay_cim, "aclose", None)) is not None:
        closers.append(close_replay_cim)
    rlcd = build_rlcd(
        settings.rlcd_backend,
        token=settings.hf_token.get_secret_value() if settings.hf_token else None,
        fixture=settings.rlcd_fixture_path,
        local_dir=settings.rlcd_local_dir,
    )
    if (close_rlcd := getattr(rlcd, "aclose", None)) is not None:
        closers.append(close_rlcd)
    components: dict[str, Any] = {
        "settings": settings,
        "catalog": catalog,
        "prices": prices,
        "engine": engine,
        "sessionmaker": sessionmaker,
        "redis": redis,
        "opa": opa,
        "policy_engine": opa,
        "dlp": dlp,
        "cache": build_response_cache(settings, redis, embedder),
        "router": ModelRouter(),
        "rlcd": rlcd,
        # Knowledge-base chunks count as screened only by the detector this scanner runs (ADR-0011).
        "retriever": build_retriever(
            settings,
            sessionmaker,
            embedder,
            injection_detector=dlp.injection.version,
            heading_fusion_profiles=[p.id for p in catalog.context_profiles if p.heading_fusion],
        ),
        "cim": cim,
        "context_guard": DeterministicContextGuard(),
        "quota": RedisQuotaService(redis, sessionmaker, prices),
        "adapters": build_adapter_registry(settings, catalog, resolver),
        "replay_adapters": build_replay_registry(settings, catalog, replay_resolver),
        "replay_cim": replay_cim,
        "resolver": resolver,
        "assurance": BaselineAssurance(dlp),
        "search": build_web_search(settings, endpoints_http),
        "trusted_proxies": parse_networks(settings.trusted_proxies),
        "closers": closers,
        "web_evidence": WebEvidenceMemory(),
    }
    unknown = set(overrides) - set(components)
    if unknown:
        raise TypeError(f"unknown service overrides: {sorted(unknown)}")
    components.update(overrides)
    return Services(**components)
