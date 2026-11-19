"""Canonical JSON and hashing for cache keys, request hashes and policy bundle hashes."""

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_hex(value: Any) -> str:
    data = value if isinstance(value, str) else canonical_json(value)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def estimate_tokens(text: str) -> int:
    """Rough tokenizer-free estimate (~4 characters per token). Always labelled `estimated`."""
    return max(1, (len(text) + 3) // 4) if text else 0
