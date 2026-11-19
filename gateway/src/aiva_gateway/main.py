"""FastAPI application factory. Run with `uvicorn --factory aiva_gateway.main:create_app`."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from aiva_gateway import __version__
from aiva_gateway.api import (
    admin,
    agents,
    approvals,
    artifacts,
    budgets,
    catalog,
    chat,
    govern_admin,
    health,
    openai_compat,
    policy,
    quality,
    recordings,
    reports,
    runs,
    session,
)
from aiva_gateway.api.errors import register_error_handlers
from aiva_gateway.api.proxy_guard import ProxyProofMiddleware
from aiva_gateway.config import Settings, get_settings
from aiva_gateway.finops.quota import RedisQuotaService
from aiva_gateway.finops.reconcile import Reconciler, reconcile_forever
from aiva_gateway.govern.publisher import ensure_opa_bundle
from aiva_gateway.reporting.worker import start_insights
from aiva_gateway.services import Services, build_services

logger = logging.getLogger(__name__)

DESCRIPTION = (
    "AiVa — AI Value Assurance. Govern every request. Optimize every token. Prove the value.\n\n"
    "The AiVa gateway. The bundled reference tenant and all of its data are synthetic."
)


def start_reconciliation(services: Services) -> "asyncio.Task[None] | None":
    """Run the FinOps reconciliation job in-process on a schedule (LLD §21.2: acceptable for the POC)."""
    interval = services.settings.reconcile_interval_seconds
    if interval <= 0 or not isinstance(services.quota, RedisQuotaService):
        return None
    reconciler = Reconciler(
        services.quota,
        services.sessionmaker,
        services.catalog,
        stale_after=services.settings.reservation_stale,
    )
    tenants = [services.catalog.tenant.tenant.id]
    return asyncio.create_task(reconcile_forever(reconciler, tenants, interval), name="aiva-reconciliation")


def start_rlcd(services: Services) -> "asyncio.Task[None] | None":
    """Load the RLCD model in the background; the heuristic RLCD serves until it is ready (ADR-0009)."""
    start = getattr(services.rlcd, "start", None)
    return asyncio.create_task(start(), name="aiva-rlcd-load") if start is not None else None


def create_app(settings: Settings | None = None, services: Services | None = None) -> FastAPI:
    resolved = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        svc = services or build_services(resolved)
        app.state.services = svc
        try:
            async with svc.sessionmaker() as session:
                await ensure_opa_bundle(svc, session)
        except Exception:
            logger.warning("Policy bundle not synchronised to OPA at startup", exc_info=True)
        starters = (start_reconciliation(svc), start_insights(svc), start_rlcd(svc))
        background = [task for task in starters if task is not None]
        yield
        for task in background:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if services is None:
            await svc.aclose()

    app = FastAPI(title="AiVa Gateway", version=__version__, description=DESCRIPTION, lifespan=lifespan)
    register_error_handlers(app)
    if resolved.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=resolved.cors_origins,
            allow_methods=["GET", "POST"],
            # A browser sends the AiVa credential only in X-AiVa-Authorization, never Authorization, which the
            # platform's edge reads (ADR-0019). Scripted clients need no CORS.
            allow_headers=["X-AiVa-Authorization", "Content-Type", "Idempotency-Key"],
            expose_headers=["Idempotent-Replayed"],
        )
    if resolved.ingress == "platform-proxy":
        # Outermost: nothing but the container's probes is served without the proxy proof (ADR-0007). An
        # empty secret matches nothing, so a misconfigured gateway refuses everything.
        secret = resolved.proxy_secret.get_secret_value() if resolved.proxy_secret else ""
        app.add_middleware(ProxyProofMiddleware, secret=secret)
    for module in (
        health,
        session,
        chat,
        policy,
        approvals,
        govern_admin,
        runs,
        budgets,
        admin,
        recordings,
        reports,
        catalog,
        agents,
        artifacts,
        quality,
        openai_compat,
    ):
        app.include_router(module.router)
    return app
