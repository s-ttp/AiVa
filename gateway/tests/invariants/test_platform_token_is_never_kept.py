"""Behind the platform proxy, `Authorization` carries the platform's token (the HF token, ADR-0019). The
gateway never reads it, so it never reaches a response, a log, a span, an audit row, a run record or Redis.
Neither does the proxy-identity secret, nor the visitor's raw address."""

import logging

from sqlalchemy import text

from aiva_gateway.db.models import Base
from tests.conftest import chat_body, console
from tests.integration.test_public_demo import APP, HF_TOKEN_CANARY, SECRET, proxied, through_proxy

VISITOR_ADDRESS = "34.201.77.13"


async def _rows_containing(db, needle: str) -> dict[str, int]:  # type: ignore[no-untyped-def]
    found = {}
    for table in Base.metadata.sorted_tables:
        count = await db.scalar(
            text(f'SELECT count(*) FROM "{table.name}" t WHERE CAST(t AS text) LIKE :needle'),
            {"needle": f"%{needle}%"},
        )
        if count:
            found[table.name] = int(count)
    return found


async def _redis_containing(redis, needle: str) -> list[str]:  # type: ignore[no-untyped-def]
    hits = []
    async for key in redis.scan_iter():
        value = await redis.get(key) if await redis.type(key) == "string" else None
        if needle in key or needle in (value or ""):
            hits.append(key)
    return hits


async def test_the_platform_token_is_never_read_or_kept(
    services, make_client, user_token, db, caplog
) -> None:  # type: ignore[no-untyped-def]
    caplog.set_level(logging.DEBUG)
    svc = proxied(services)
    responses = []
    async with make_client(svc=svc, headers=through_proxy(ip=VISITOR_ADDRESS)) as client:
        started = await client.post("/v1/auth/public/session")
        responses.append(started)
        visitor = started.json()["access_token"]
    async with make_client(svc=svc, headers=through_proxy(visitor, ip=VISITOR_ADDRESS)) as client:
        ran = await client.post("/v1/ai/chat", json=chat_body("Explain roaming charges.", **console(APP)))
        responses += [
            ran,
            await client.get("/v1/auth/me"),
            await client.get(f"/v1/runs/{ran.json()['run_id']}"),
        ]
    async with make_client(svc=svc, headers=through_proxy(user_token("u-tester"))) as client:
        responses.append(await client.post("/v1/ai/chat", json=chat_body("Explain roaming.", **console(APP))))
    # Refusals must not echo it either: no proof, and a bad AiVa credential behind a valid proof.
    async with make_client(svc=svc, headers={"Authorization": f"Bearer {HF_TOKEN_CANARY}"}) as client:
        responses.append(await client.get("/v1/auth/me"))
    async with make_client(svc=svc, headers=through_proxy("not-a-token")) as client:
        responses.append(await client.get("/v1/auth/me"))

    assert [r.status_code for r in responses] == [201, 200, 200, 200, 200, 401, 401]
    # The scans find what is there: the session's audit row and its rate-limit counter.
    assert (await _rows_containing(db, "auth.public_session")).get("audit_log")
    assert await _redis_containing(services.redis, "aiva:public-session:")
    for needle in (HF_TOKEN_CANARY, SECRET, VISITOR_ADDRESS):
        assert not [r for r in responses if needle in r.text or needle in str(r.headers)], needle
        assert needle not in caplog.text
        assert await _rows_containing(db, needle) == {}, needle
        assert await _redis_containing(services.redis, needle) == [], needle
