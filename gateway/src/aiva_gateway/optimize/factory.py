"""Build the Optimize stage implementations from settings (wired in services.py)."""

from collections.abc import Collection

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aiva_gateway.catalog import Catalog
from aiva_gateway.config import Settings
from aiva_gateway.inference.endpoints import EndpointResolver
from aiva_gateway.inference.factory import build_openai_compatible, validate_mode
from aiva_gateway.inference.replay import ReplayOrSimulatedCim, replay_settings
from aiva_gateway.optimize.cache import NullResponseCache, RedisResponseCache
from aiva_gateway.optimize.cim import SimulatedContextIntelligence
from aiva_gateway.optimize.cim_live import CIM_EXTRA_PARAMS_BY_CONTRACT, LiveContextIntelligence
from aiva_gateway.optimize.embeddings import Embedder, build_embedder, build_reranker
from aiva_gateway.optimize.retrieval import HybridRetriever, NullRetriever
from aiva_gateway.pipeline.interfaces import ContextIntelligence, ResponseCache, Retriever


def build_shared_embedder(settings: Settings) -> Embedder | None:
    semantic = settings.response_cache_enabled and settings.semantic_cache_enabled
    if not (semantic or settings.retrieval_enabled):
        return None
    return build_embedder(settings.embedding_backend)  # models load lazily on first use


def build_response_cache(settings: Settings, redis: Redis, embedder: Embedder | None) -> ResponseCache:
    if not settings.response_cache_enabled:
        return NullResponseCache()
    return RedisResponseCache(
        redis,
        embedder=embedder if settings.semantic_cache_enabled else None,
        semantic_threshold=settings.semantic_cache_threshold,
        # Switching mode (e.g. simulated -> live) must never serve answers produced under the other mode.
        namespace=f"aiva:cache:{settings.inference_mode}",
    )


def build_retriever(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    embedder: Embedder | None,
    *,
    injection_detector: str,
    heading_fusion_profiles: Collection[str] = (),
) -> Retriever:
    if not settings.retrieval_enabled or embedder is None:
        return NullRetriever()
    reranker = None if settings.reranker_backend == "none" else build_reranker(settings.reranker_backend)
    return HybridRetriever(
        sessionmaker,
        embedder=embedder,
        reranker=reranker,
        injection_detector=injection_detector,
        heading_fusion=settings.retrieval_heading_fusion,
        heading_fusion_profiles=heading_fusion_profiles,
    )


def build_context_intelligence(
    settings: Settings, catalog: Catalog, resolver: EndpointResolver
) -> ContextIntelligence:
    mode = validate_mode(settings)
    model = catalog.model(catalog.models.cim_model)
    if mode == "simulated" or model is None or model.id in settings.simulated_models:
        return SimulatedContextIntelligence(catalog.context_profile)
    extra = CIM_EXTRA_PARAMS_BY_CONTRACT[model.cim_contract]
    return LiveContextIntelligence(
        build_openai_compatible(model, settings, resolver, extra_params=extra),
        model.cim_contract,
        constrain_ids=settings.cim_v2_constrain_ids,
    )


def build_replay_context_intelligence(
    settings: Settings, catalog: Catalog, resolver: EndpointResolver
) -> ContextIntelligence:
    """Per-run replay (ADR-0019): the CIM's recorded packages, a miss answered by the simulated CIM."""
    simulated = SimulatedContextIntelligence(catalog.context_profile)
    replay = build_context_intelligence(replay_settings(settings), catalog, resolver)
    if isinstance(replay, SimulatedContextIntelligence):
        return replay
    return ReplayOrSimulatedCim(replay, simulated)
