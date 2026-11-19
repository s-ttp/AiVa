"""RLCD in the pipeline on recorded model outputs (ML's parity rows): it can only tighten, never lower."""

from sqlalchemy import select

from aiva_gateway.db.models import ClassificationEventRow, ModelAttemptRow
from aiva_gateway.optimize.rlcd_model import build_rlcd
from aiva_gateway.pipeline.interfaces import CachedResponse, CacheKey
from tests.conftest import console, with_overrides
from tests.unit.test_rlcd_model import PARITY, VERSION, messages


async def _event(db, run_id: str) -> ClassificationEventRow:  # type: ignore[no-untyped-def]
    return (
        await db.execute(select(ClassificationEventRow).where(ClassificationEventRow.run_id == run_id))
    ).scalar_one()


def _rlcd(services, **overrides):  # type: ignore[no-untyped-def]
    return with_overrides(services, rlcd=build_rlcd("fixture", token=None, fixture=PARITY), **overrides)


class SpyCache:
    """Records every store; serves exact hits from what it stored."""

    cache_type = "exact"

    def __init__(self) -> None:
        self.stored: list[tuple[CacheKey, CachedResponse]] = []

    async def lookup(self, key: CacheKey) -> CachedResponse | None:
        return next((entry for k, entry in self.stored if k.key_hash == key.key_hash), None)

    async def store(self, key: CacheKey, entry: CachedResponse, ttl_seconds: int) -> None:
        self.stored.append((key, entry))


async def test_a_confidential_prediction_tightens_a_dlp_clean_request_to_the_sovereign_path(
    services, tester_client, db
) -> None:  # type: ignore[no-untyped-def]
    # "Why did my bill increase by AED 31.50 this month?": no DLP finding; the model says confidential.
    payload = {"messages": messages("rl-d8e4d890097f6c84"), **console("gct-support-chatbot")}
    async with tester_client(svc=_rlcd(services)) as client:
        body = (await client.post("/v1/ai/chat", json=payload)).json()
    assert body["status"] == "completed", body.get("error")
    assert body["policy"]["effective_classification"] == "confidential"
    assert body["execution_path"] == "sovereign-direct" and body["selected_model"] == "gct-qwen-finetuned"
    attempts = (
        (await db.execute(select(ModelAttemptRow).where(ModelAttemptRow.run_id == body["run_id"])))
        .scalars()
        .all()
    )
    assert {a.model_id for a in attempts} == {"gct-qwen-finetuned"}  # no CIM, no external model

    event = await _event(db, body["run_id"])
    assert event.classifier_version == VERSION and event.sensitivity == "confidential"
    assert event.task_category == "qa"
    assert event.complexity is None  # 0.55 is below the 0.80 threshold: reported as unclassified
    assert event.review_needed
    assert event.confidences["provenance"] == "measured"
    assert event.confidences["rlcd"] == event.confidences["task_category"] > 0.99


async def test_a_miss_never_lowers_the_classification(services, tester_client, db) -> None:  # type: ignore[no-untyped-def]
    # Parity row 57713: gold confidential, the model says internal at 0.61. DLP and policy still decide.
    payload = {"messages": messages("rl-57713fb159c8f9ba"), **console("gct-fraud-investigator")}
    async with tester_client(svc=_rlcd(services)) as client:
        body = (await client.post("/v1/ai/chat", json=payload)).json()
    assert body["status"] == "completed", body.get("error")
    assert body["policy"]["effective_classification"] == "confidential"
    assert body["execution_path"] == "sovereign-direct"
    event = await _event(db, body["run_id"])
    assert event.classifier_version == VERSION and event.sensitivity == "confidential"


async def test_without_a_prediction_the_run_is_routed_and_recorded_by_the_heuristic(
    services, tester_client, db
) -> None:  # type: ignore[no-untyped-def]
    payload = {
        "messages": [{"role": "user", "content": "Which bundles include unlimited SMS?"}],
        **console("gct-support-chatbot"),
    }
    async with tester_client(svc=_rlcd(services)) as client:
        body = (await client.post("/v1/ai/chat", json=payload)).json()
    assert body["status"] == "completed", body.get("error")
    event = await _event(db, body["run_id"])
    assert event.classifier_version == "heuristic-v0" and event.confidences == {}


# --- Response cache after RLCD tightening (Govern review of #48) ------------------------------------------


async def test_an_answer_produced_after_rlcd_tightening_is_never_stored(services, tester_client) -> None:  # type: ignore[no-untyped-def]
    # A DLP-clean, non-personal RAG question on a shared-cache application: its lookup key is the internal,
    # role-shared scope. The model raises it to confidential, so the sovereign answer must not be stored
    # under that key, where a later internal lookup (or another user of the role) would receive it.
    cache = SpyCache()
    payload = {"messages": messages("rl-b6d9b3705138cf03"), **console("gct-knowledge-portal")}
    async with tester_client(svc=_rlcd(services, cache=cache)) as client:
        first = (await client.post("/v1/ai/chat", json=payload)).json()
        again = (await client.post("/v1/ai/chat", json=payload)).json()
    assert first["status"] == "completed", first.get("error")
    assert first["policy"]["effective_classification"] == "confidential"
    assert first["execution_path"] == "sovereign-direct"
    assert cache.stored == []
    assert again["execution_path"] == "sovereign-direct" and again["cache"]["hit"] is False


async def test_an_untightened_answer_is_still_cached_at_its_classification(services, tester_client) -> None:  # type: ignore[no-untyped-def]
    cache = SpyCache()
    payload = {"messages": messages("rl-1fefb6e639f9441d"), **console("gct-knowledge-portal")}
    async with tester_client(svc=_rlcd(services, cache=cache)) as client:
        first = (await client.post("/v1/ai/chat", json=payload)).json()
        again = (await client.post("/v1/ai/chat", json=payload)).json()
    assert first["status"] == "completed", first.get("error")
    assert first["policy"]["effective_classification"] == "internal"
    [(key, _entry)] = cache.stored
    assert key.scope.endswith("/internal") and "/roles:" in key.scope
    assert again["execution_path"] == "cache-return"


async def test_confidential_answers_are_cached_per_end_user_only_and_never_without_one(
    services, tester_client, app_client
) -> None:  # type: ignore[no-untyped-def]
    # Existing rule (LLD §11, ADR-0009): confidential or above is personal: per-user scope, no semantic
    # reuse, and no entry at all without an end-user pseudonym.
    cache = SpyCache()
    payload = {"messages": messages("rl-57713fb159c8f9ba")}
    svc = _rlcd(services, cache=cache)
    async with tester_client(svc=svc) as client:
        body = (
            await client.post("/v1/ai/chat", json={**payload, **console("gct-fraud-investigator")})
        ).json()
    assert body["policy"]["effective_classification"] == "confidential"
    [(key, _entry)] = cache.stored
    assert key.scope.endswith("/confidential") and "/user:" in key.scope and key.semantic is None
    async with app_client("gct-fraud-investigator", svc=svc) as client:
        body = (await client.post("/v1/ai/chat", json=payload)).json()
    assert body["status"] == "completed", body.get("error")
    assert len(cache.stored) == 1  # the application-credential run stored nothing
