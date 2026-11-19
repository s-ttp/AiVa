from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from aiva_gateway.api.deps import ServicesDep
from aiva_gateway.govern.publisher import ensure_opa_bundle

router = APIRouter(tags=["health"])


@router.get("/health/live", summary="Liveness probe")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get(
    "/health/ready",
    summary="Readiness probe: database, Redis, OPA, the policy bundle and, if configured, the RLCD model",
)
async def ready(services: ServicesDep) -> JSONResponse:
    checks: dict[str, str] = {}
    try:
        async with services.sessionmaker() as session:
            await session.execute(text("select 1"))
            checks["database"] = "ok"
            if await services.opa.ready():
                bundle_hash = await ensure_opa_bundle(services, session)
                checks["policy_engine"] = "ok"
                checks["policy_bundle"] = bundle_hash[:12] if bundle_hash else "missing"
            else:
                checks["policy_engine"] = "unavailable"
    except Exception as exc:
        checks.setdefault("database", f"error: {type(exc).__name__}")
    try:
        await services.redis.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {type(exc).__name__}"
    if services.settings.rlcd_backend == "model":
        # While the RLCD model loads, runs would be routed by the heuristic, unlike the recordings (ADR-0009):
        # not ready, so the proxy answers GATEWAY_STARTING. A failed load is ready but shows `failed`: the
        # labelled heuristic serves rather than taking the gateway down.
        checks["rlcd_model"] = getattr(services.rlcd, "status", "loading")
    healthy = (
        all(v == "ok" for k, v in checks.items() if k not in {"policy_bundle", "rlcd_model"})
        and checks.get("policy_bundle", "missing") != "missing"
        and checks.get("rlcd_model") != "loading"
    )
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "degraded", "checks": checks},
    )
