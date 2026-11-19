"""Read-only catalog for the Test Console (an addition to the LLD API list)."""

from fastapi import APIRouter

from aiva_gateway.api.deps import CallerDep, ServicesDep
from aiva_gateway.api.errors import ERROR_RESPONSES
from aiva_gateway.catalog.models import Scenario

router = APIRouter(tags=["catalog"])


@router.get(
    "/v1/catalog/scenarios",
    response_model=list[Scenario],
    responses=ERROR_RESPONSES,
    summary="The ten locked synthetic scenarios (LLD §15)",
)
async def scenarios(caller: CallerDep, services: ServicesDep) -> list[Scenario]:
    return sorted(services.catalog.scenarios, key=lambda s: s.number)
