from decimal import Decimal

import pytest

from aiva_gateway.core.canonical import canonical_json, sha256_hex
from aiva_gateway.core.enums import Classification, most_restrictive
from aiva_gateway.core.ids import ulid
from aiva_gateway.core.money import token_cost_micros, usd_to_micros


def test_most_restrictive_wins() -> None:
    assert (
        most_restrictive(Classification.PUBLIC, Classification.CONFIDENTIAL, None)
        is Classification.CONFIDENTIAL
    )
    assert (
        most_restrictive(Classification.SOVEREIGN_ONLY, Classification.RESTRICTED)
        is Classification.SOVEREIGN_ONLY
    )
    assert most_restrictive(None, None) is Classification.INTERNAL


def test_sovereign_threshold() -> None:
    assert not Classification.INTERNAL.requires_sovereign_path
    assert Classification.CONFIDENTIAL.requires_sovereign_path
    assert Classification.SOVEREIGN_ONLY.requires_sovereign_path


def test_money_is_integer_micros() -> None:
    assert usd_to_micros("4.00") == 4_000_000
    assert usd_to_micros(Decimal("0.0000005")) == 1  # rounds half up
    assert token_cost_micros(1_000, 4_000_000) == 4_000
    assert token_cost_micros(1, 1) == 0
    with pytest.raises(ValueError):
        token_cost_micros(-1, 1)


def test_ulids_are_sortable_and_unique() -> None:
    values = [ulid() for _ in range(200)]
    assert len(set(values)) == 200
    assert all(len(v) == 26 for v in values)


def test_canonical_hash_is_key_order_independent() -> None:
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})
    assert sha256_hex({"b": 1, "a": 2}) == sha256_hex({"a": 2, "b": 1})
