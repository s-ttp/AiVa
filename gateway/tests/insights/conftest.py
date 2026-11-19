"""Shared Insights fixtures: one synthetic traffic run (simulated inference) and a fresh aggregation."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

import pytest

from aiva_gateway.optimize.cache import RedisResponseCache
from aiva_gateway.reporting.aggregate import HOUR, Aggregator, floor_hour
from aiva_gateway.reporting.traffic import TrafficReport, generate_traffic, plan_traffic
from aiva_gateway.services import Services


@dataclass(frozen=True)
class GeneratedTraffic:
    report: TrafficReport
    start: datetime
    end: datetime


def aggregator_for(services: Services) -> Aggregator:
    return Aggregator(services.sessionmaker, services.catalog, services.prices)


async def refresh_window(services: Services, start: datetime, end: datetime) -> None:
    result = await aggregator_for(services).refresh("gct", triggered_by="test", start=start, end=end)
    assert result.status == "completed", result.error


@pytest.fixture(scope="session")
async def traffic(services: Services) -> AsyncIterator[GeneratedTraffic]:
    # The shared test settings disable the response cache; the acceptance run needs one full cache hit.
    cache = RedisResponseCache(services.redis, embedder=None, namespace=f"aiva:cache:insights-{id(services)}")
    with_cache = replace(services, cache=cache)
    start = floor_hour(datetime.now().astimezone())
    report = await generate_traffic(with_cache, plan=plan_traffic(40, seed=11), tenant_id="gct")
    end = floor_hour(datetime.now().astimezone()) + HOUR
    await refresh_window(services, start - timedelta(hours=1), end)
    yield GeneratedTraffic(report=report, start=start, end=end)
