"""User JWTs. The POC issuer is HS256; an OIDC/JWKS verifier can replace `decode_user_token` later."""

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from aiva_gateway.config import Settings


def mint_user_token(
    settings: Settings,
    *,
    user_id: str,
    tenant_id: str,
    ttl_seconds: int = 3600,
    session_id: str | None = None,
) -> str:
    """A user JWT. `session_id` (claim `sid`) marks a public demo session: the visitor's pseudonym is
    derived from it, so each browser session is its own visitor (ADR-0019)."""
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": user_id,
        "tid": tenant_id,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "exp": now + timedelta(seconds=ttl_seconds),
    }
    if session_id is not None:
        claims["sid"] = session_id
    return jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm="HS256")


def new_session_id() -> str:
    return secrets.token_urlsafe(16)


def decode_user_token(settings: Settings, token: str) -> dict[str, Any]:
    return jwt.decode(
        token,
        settings.jwt_secret.get_secret_value(),
        algorithms=["HS256"],
        audience=settings.jwt_audience,
        issuer=settings.jwt_issuer,
        options={"require": ["sub", "tid", "exp", "iss", "aud"]},
    )


def pseudonymise(key: str, user_id: str, session_id: str | None = None) -> str:
    """Stable pseudonym used in run and reporting tables instead of the user ID (LLD §21.8). A public demo
    session is pseudonymised per session (ADR-0019)."""
    subject = user_id if session_id is None else f"{user_id}:session:{session_id}"
    return "usr_" + hmac.new(key.encode(), subject.encode(), hashlib.sha256).hexdigest()[:16]
