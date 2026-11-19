"""`GET /v1/admin/recordings`: a record-mode gateway's provider exchanges, for administrators (ADR-0009)."""

import json
from pathlib import Path

from sqlalchemy import select

from aiva_gateway.auth.principal import Caller
from aiva_gateway.auth.rbac import GRANTS, Permission, granted
from aiva_gateway.core.enums import Role
from aiva_gateway.db.models import AuditLogRow
from aiva_gateway.inference.recording import RecordingStore
from tests.conftest import ADMIN, SECOND_ADMIN, with_overrides

PATH = "/v1/admin/recordings"
REPLY = '{"choices": [{"message": {"content": "Synthetic answer."}}]}'


def recording_services(services, directory: Path):  # type: ignore[no-untyped-def]
    settings = services.settings.model_copy(update={"inference_mode": "record", "recordings_dir": directory})
    return with_overrides(services, settings=settings)


async def test_only_administrators_and_only_in_record_mode(make_client, user_token, services) -> None:  # type: ignore[no-untyped-def]
    async with make_client() as client:
        assert (await client.get(PATH)).status_code == 401
    # Platform administrators only, refused before the mode is revealed.
    for user in ("u-tester", SECOND_ADMIN, "u-auditor", "u-finance"):  # the visitor: see the grant test
        async with make_client(user_token(user)) as client:
            denied = await client.get(PATH)
        assert denied.status_code == 403 and denied.json()["error"]["code"] == "PERMISSION_DENIED", user
    async with make_client(user_token(ADMIN)) as client:  # this gateway runs simulated inference
        missing = await client.get(PATH)
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "NOT_FOUND"


async def test_exports_scrubbed_bodies_withholds_the_rest_and_audits(
    make_client, user_token, services, db, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    store = RecordingStore(tmp_path, label="gemini-flash-lite")
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/x:generateContent?alt=sse&key=not-a-real-key"
    )
    body = json.dumps({"contents": [{"parts": [{"text": "Synthetic question."}]}]}).encode()
    saved = store.save("POST", url, body, 200, "text/event-stream", REPLY)
    # A file holding one of the gateway's own secrets, a hand-edited URL credential, and a stray file.
    secret = services.settings.jwt_secret.get_secret_value()
    RecordingStore(tmp_path, label="claude-flagship").save(
        "POST", "https://api.anthropic.com/v1/messages", b"{}", 200, "application/json", f"echo {secret}"
    )
    tampered = json.loads(saved.read_text())
    tampered["request"]["url"] = "https://router.huggingface.co/v1/chat/completions?api_key=leak"
    tampered["headers"] = {"authorization": "Bearer leak"}
    (tmp_path / "deepseek-v4-1-flash-0123456789abcdef01234567.json").write_text(json.dumps(tampered))
    (tmp_path / "notes.json").write_text("{}")

    async with make_client(user_token(ADMIN), svc=recording_services(services, tmp_path)) as client:
        response = await client.get(PATH)
    assert response.status_code == 200, response.json()
    bundle = response.json()
    assert (bundle["inference_mode"], bundle["withheld"]) == ("record", 2)
    by_label = {entry["name"].rsplit("-", 1)[0]: entry for entry in bundle["recordings"]}
    assert set(by_label) == {"gemini-flash-lite", "deepseek-v4-1-flash"}
    for entry in bundle["recordings"]:  # what the recording script checks before it writes a fixture
        assert set(entry) == {"name", "content"}
        assert set(entry["content"]) == {"request", "response"}
        assert set(entry["content"]["request"]) == {"method", "url", "body"}
        assert set(entry["content"]["response"]) == {"status_code", "content_type", "body"}
    gemini = by_label["gemini-flash-lite"]
    assert gemini["name"] == saved.name
    assert gemini["content"] == json.loads(saved.read_text())  # byte for byte the fixture replay reads
    assert gemini["content"]["request"]["url"].endswith("alt=sse")  # no key
    deepseek = by_label["deepseek-v4-1-flash"]["content"]["request"]
    assert deepseek["url"] == "https://router.huggingface.co/v1/chat/completions"
    assert "leak" not in response.text and secret not in response.text

    [audit] = (
        await db.execute(select(AuditLogRow).where(AuditLogRow.action == "recordings.export"))
    ).scalars()
    assert audit.actor_kind == "user" and audit.actor_id != ADMIN  # a pseudonym, never the user ID
    assert audit.details == {
        "exported": 2,
        "withheld": 2,
        "labels": {"gemini-flash-lite": 1, "deepseek-v4-1-flash": 1},
    }


def test_only_the_platform_admin_role_holds_the_export_permission() -> None:
    assert GRANTS[Permission.RECORDINGS_EXPORT] == {Role.PLATFORM_ADMIN}
    for role in Role:
        caller = Caller(kind="user", tenant_id="gct", pseudonym="p", roles=frozenset({role}))
        assert granted(caller, Permission.RECORDINGS_EXPORT) is (role is Role.PLATFORM_ADMIN), role
    assert not granted(Caller(kind="application", tenant_id="gct"), Permission.RECORDINGS_EXPORT)
