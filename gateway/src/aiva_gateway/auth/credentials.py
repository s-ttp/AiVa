"""Application credentials: `aiva_<key_id>.<secret>`. Only an HMAC of the secret is stored."""

import hashlib
import hmac
import secrets

PREFIX = "aiva_"


def hash_secret(secret: str, pepper: str) -> str:
    return hmac.new(pepper.encode(), secret.encode(), hashlib.sha256).hexdigest()


def new_credential(pepper: str) -> tuple[str, str, str]:
    """Return (key_id, plaintext credential, secret HMAC). Show the plaintext once; store only the HMAC."""
    key_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    return key_id, f"{PREFIX}{key_id}.{secret}", hash_secret(secret, pepper)


def parse_credential(token: str) -> tuple[str, str] | None:
    if not token.startswith(PREFIX) or "." not in token:
        return None
    key_id, _, secret = token[len(PREFIX) :].partition(".")
    if not key_id or not secret:
        return None
    return key_id, secret


def verify_secret(secret: str, expected_hmac: str, pepper: str) -> bool:
    return hmac.compare_digest(hash_secret(secret, pepper), expected_hmac)
