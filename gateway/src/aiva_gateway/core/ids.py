"""Sortable, immutable identifiers (ULID-style, Crockford base32)."""

import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid() -> str:
    """Return a 26-character ULID: 48-bit millisecond timestamp + 80 bits of randomness."""
    value = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    chars = []
    for _ in range(26):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def new_id(prefix: str) -> str:
    return f"{prefix}_{ulid()}"


def run_id() -> str:
    return new_id("run")


def attempt_id() -> str:
    return new_id("att")


def reservation_id() -> str:
    return new_id("res")


def ledger_id() -> str:
    return new_id("led")
