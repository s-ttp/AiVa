"""Test fixtures. Integration tests need Postgres, Redis and OPA (see CONTRIBUTING.md "Local services")."""

import asyncio
import os
import socket
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import httpx2
import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from aiva_gateway.auth.tokens import mint_user_token
from aiva_gateway.catalog.seed import create_application_credential, seed_reference_data
from aiva_gateway.config import REPO_ROOT, Settings
from aiva_gateway.main import create_app
from aiva_gateway.services import Services, build_services

TEST_DATABASE_URL = os.environ.get(
    "AIVA_TEST_DATABASE_URL", "postgresql+asyncpg://aiva:aiva@127.0.0.1:5432/aiva_test"
)
TEST_REDIS_URL = os.environ.get("AIVA_TEST_REDIS_URL", "redis://127.0.0.1:6379/15")
TEST_OPA_URL = os.environ.get("AIVA_TEST_OPA_URL", "http://127.0.0.1:8181")

# A peer address inside each application's trusted network (config/tenant.yaml).
PEERS = {
    "gct-support-chatbot": "10.20.1.15",
    "gct-fraud-investigator": "10.30.10.20",
    "gct-noc-assistant": "10.50.1.1",
    "gct-coding-assistant": "10.40.1.1",
    "gct-knowledge-portal": "10.40.1.2",
    "gct-incident-agent": "10.50.1.2",
    "gct-doc-copilot": "10.60.1.1",
}
CONSOLE_IP = "192.0.2.10"


@pytest.fixture(scope="session", autouse=True)
def no_deployment_secrets_from_env() -> Iterator[None]:
    """Deployment secrets in the developer's environment (the cloud sessions that run the deployed smoke tests
    hold them) must not configure the test gateway: Settings reads AIVA_* from the environment. Session-scoped
    and autouse, so it applies before the session `settings` fixture is built. tests/e2e is outside this
    conftest and keeps them."""
    with pytest.MonkeyPatch.context() as mp:
        for name in ("AIVA_DEMO_PASSCODE", "AIVA_PROXY_SECRET", "AIVA_SEARCH_API_KEY", "AIVA_OPENAI_API_KEY"):
            mp.delenv(name, raising=False)
        yield


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(
        environment="test",
        database_url=TEST_DATABASE_URL,
        redis_url=TEST_REDIS_URL,
        opa_url=TEST_OPA_URL,
        config_dir=REPO_ROOT / "config",
        # Most tests repeat identical prompts and assert on per-run attempts; cache tests enable it.
        response_cache_enabled=False,
        # Dependency-free stand-ins: CI never downloads the ONNX models.
        embedding_backend="hashing",
        reranker_backend="lexical",
    )


def _migrate(database_url: str) -> None:
    config = Config(str(Path(REPO_ROOT, "gateway", "alembic.ini")))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")


@pytest.fixture(scope="session")
async def services(settings: Settings) -> AsyncIterator[Services]:
    engine = create_async_engine(settings.database_url.get_secret_value())
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await engine.dispose()
    await asyncio.to_thread(_migrate, settings.database_url.get_secret_value())
    svc = build_services(settings)
    await svc.redis.flushdb()
    async with svc.sessionmaker() as session, session.begin():
        await seed_reference_data(svc, session)
    yield svc
    await svc.aclose()


@pytest.fixture(scope="session")
async def credentials(services: Services) -> dict[str, str]:
    issued = {}
    async with services.sessionmaker() as session, session.begin():
        for app in services.catalog.tenant.applications:
            issued[app.id] = await create_application_credential(services, session, app.id, "test")
    return issued


@pytest.fixture
async def db(services: Services) -> AsyncIterator[AsyncSession]:
    async with services.sessionmaker() as session:
        yield session


ClientFactory = Callable[..., AsyncClient]


@pytest.fixture
def make_client(services: Services) -> ClientFactory:
    def _make(
        token: str | None = None,
        *,
        peer: str = "127.0.0.1",
        svc: Services | None = None,
        headers: dict[str, str] | None = None,
    ) -> AsyncClient:
        active = svc or services
        app = create_app(settings=active.settings, services=active)
        app.state.services = active
        all_headers = {"Authorization": f"Bearer {token}"} if token else {}
        all_headers.update(headers or {})
        return AsyncClient(
            transport=ASGITransport(app=app, client=(peer, 50000)),
            base_url="http://gateway",
            headers=all_headers,
        )

    return _make


@pytest.fixture
def user_token(settings: Settings) -> Callable[[str], str]:
    return lambda user_id: mint_user_token(settings, user_id=user_id, tenant_id="gct")


@pytest.fixture
def app_client(make_client: ClientFactory, credentials: dict[str, str]) -> Callable[..., AsyncClient]:
    def _make(application_id: str, **kwargs: Any) -> AsyncClient:
        kwargs.setdefault("peer", PEERS[application_id])
        return make_client(credentials[application_id], **kwargs)

    return _make


@pytest.fixture
def tester_client(make_client: ClientFactory, user_token: Callable[[str], str]) -> Callable[..., AsyncClient]:
    def _make(**kwargs: Any) -> AsyncClient:
        return make_client(user_token("u-tester"), **kwargs)

    return _make


def with_overrides(services: Services, **overrides: Any) -> Services:
    return replace(services, **overrides)


def chat_body(content: str, **extra: Any) -> dict[str, Any]:
    return {"messages": [{"role": "user", "content": content}], **extra}


def console(application_id: str, ip: str = CONSOLE_IP) -> dict[str, Any]:
    return {"test_profile": {"application_id": application_id, "simulated_source_ip": ip}}


ADMIN, SECOND_ADMIN = "u-platform-admin", "u-tenant-admin"


def seeded_policies(services: Services) -> list[dict[str, Any]]:
    return [p.model_dump(mode="json", exclude_none=True) for p in services.catalog.policies]


@pytest.fixture
async def policy_versions(
    services: Services, make_client: ClientFactory, user_token: Callable[[str], str]
) -> AsyncIterator[Callable[..., Any]]:
    """Publish policy versions for a test through the API; roll back to the starting version afterwards."""
    from aiva_gateway.govern.publisher import latest_published

    async with services.sessionmaker() as session:
        start = await latest_published(session, "gct")
    assert start is not None

    async def _publish(extra: list[dict[str, Any]], *, base: list[dict[str, Any]] | None = None) -> int:
        policies = (seeded_policies(services) if base is None else base) + extra
        async with make_client(user_token(ADMIN)) as client:
            draft = await client.post("/v1/admin/policies", json={"policies": policies, "notes": "test"})
            assert draft.status_code == 201, draft.json()
            version = draft.json()["version"]
        async with make_client(user_token(SECOND_ADMIN)) as client:
            published = await client.post(
                "/v1/admin/policies/publish", json={"version": version, "approval_note": "test publish"}
            )
            assert published.status_code == 200, published.json()
        return int(version)

    yield _publish
    async with services.sessionmaker() as session:
        current = await latest_published(session, "gct")
    if current is not None and current.version != start.version:
        async with make_client(user_token(SECOND_ADMIN)) as client:
            restored = await client.post(
                "/v1/admin/policies/publish", json={"version": start.version, "approval_note": "test restore"}
            )
            assert restored.status_code == 200, restored.json()


# --- Per-run execution mode (ADR-0019) ------------------------------------------------------------------

# A gateway configured live, with live credentials present (none of them real).
LIVE_SETTINGS: dict[str, Any] = {
    "inference_mode": "live",
    "simulated_models": [],
    "endpoint_namespace": "s-ttp",
    "hf_token": SecretStr("hf_test_not_a_real_token"),
    "anthropic_api_key": SecretStr("sk-ant-test-not-a-real-key"),
    "gemini_api_key": SecretStr("gemini-test-not-a-real-key"),
}
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


@pytest.fixture
async def live_services(settings: Settings) -> AsyncIterator[Services]:
    svc = build_services(settings.model_copy(update=LIVE_SETTINGS))
    yield svc
    await svc.aclose()


def _proxy_ports() -> set[int]:
    ports = set()
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        if value := os.environ.get(name):
            port = urlsplit(value).port
            if port:
                ports.add(port)
    return ports


@pytest.fixture
def outbound(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Records, and refuses, every outbound request: at the HTTP layer (httpx and httpx2 transports, by
    destination host, so a proxy in the path changes nothing), and at the socket layer (a DNS lookup or a
    connection to a non-local host, or to the configured HTTP proxy). Local services (Postgres, Redis, OPA)
    stay reachable."""
    attempts: list[str] = []
    proxy_ports = _proxy_ports()

    def refuse(target: str) -> None:
        attempts.append(target)
        raise OSError("outbound network disabled in this test")

    for module in (httpx, httpx2):
        real_handle = module.AsyncHTTPTransport.handle_async_request

        async def handle(self, request, _real=real_handle):  # type: ignore[no-untyped-def]
            if request.url.host not in LOCAL_HOSTS:
                refuse(str(request.url.host))
            return await _real(self, request)

        monkeypatch.setattr(module.AsyncHTTPTransport, "handle_async_request", handle)

    real_getaddrinfo = socket.getaddrinfo
    real_connect = asyncio.base_events.BaseEventLoop.create_connection

    def getaddrinfo(host, *args, **kwargs):  # type: ignore[no-untyped-def]
        if host is not None and str(host) not in LOCAL_HOSTS:
            refuse(str(host))
        return real_getaddrinfo(host, *args, **kwargs)

    async def create_connection(self, factory, host=None, port=None, *args, **kwargs):  # type: ignore[no-untyped-def]
        if (host is not None and str(host) not in LOCAL_HOSTS) or port in proxy_ports:
            refuse(f"{host}:{port}")
        return await real_connect(self, factory, host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "create_connection", create_connection)
    yield attempts
