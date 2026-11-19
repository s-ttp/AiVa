"""Money as integer micro-USD (ADR-0006). No floats in cost paths."""

from decimal import ROUND_HALF_UP, Decimal

MICROS_PER_USD = 1_000_000
TOKENS_PER_PRICE_UNIT = 1_000_000


def usd_to_micros(amount: Decimal | str | int) -> int:
    return int((Decimal(amount) * MICROS_PER_USD).to_integral_value(rounding=ROUND_HALF_UP))


def micros_to_usd(micros: int) -> Decimal:
    return Decimal(micros) / MICROS_PER_USD


def token_cost_micros(tokens: int, micros_per_million_tokens: int) -> int:
    """Cost of `tokens` at a per-million-token price, rounded half up to the nearest micro-USD."""
    if tokens < 0 or micros_per_million_tokens < 0:
        raise ValueError("tokens and price must be non-negative")
    numerator = tokens * micros_per_million_tokens
    return (numerator + TOKENS_PER_PRICE_UNIT // 2) // TOKENS_PER_PRICE_UNIT
