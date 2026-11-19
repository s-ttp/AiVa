"""Authentication returns its database connection before the handler runs.

Under load, a request that held its authentication session while the handler opened its own needed two
pooled connections at once; with as many concurrent requests as the pool allows, every request held one
and waited for a second until the pool timeout (500). With a one-connection pool such a request cannot
complete at all, so these tests run on one.
"""

import asyncio
from collections.abc import AsyncIterator, Callable

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from aiva_gateway.db.session import create_sessionmaker
from aiva_gateway.services import Services
from tests.conftest import chat_body, with_overrides


@pytest.fixture
async def one_connection(services: Services) -> AsyncIterator[Services]:
    engine = create_async_engine(
        services.settings.database_url.get_secret_value(), pool_size=1, max_overflow=0, pool_timeout=5
    )
    try:
        yield with_overrides(services, engine=engine, sessionmaker=create_sessionmaker(engine))
    finally:
        await engine.dispose()


async def test_chat_needs_one_connection_at_a_time(
    app_client: Callable[..., AsyncClient], one_connection: Services
) -> None:
    async with app_client("gct-support-chatbot", svc=one_connection) as c:
        response = await c.post("/v1/ai/chat", json=chat_body("What are the Europe roaming rates?"))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"


async def test_concurrent_requests_queue_instead_of_exhausting_the_pool(
    app_client: Callable[..., AsyncClient], one_connection: Services
) -> None:
    async with app_client("gct-support-chatbot", svc=one_connection) as c:
        responses = await asyncio.gather(
            *(c.post("/v1/ai/chat", json=chat_body(f"Europe roaming rates, case {i}?")) for i in range(4))
        )
    assert [r.status_code for r in responses] == [200] * 4
