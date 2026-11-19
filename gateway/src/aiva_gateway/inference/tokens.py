"""Conservative upper bounds on provider-counted input tokens, for budget reservations (LLD §12).

Budget caps hold exactly only while actual spend stays within the reservation, so the reservation and the
preflight coverage check need an *upper bound* on the tokens a provider will bill, not an estimate: the
4-characters-per-token estimate (`core.canonical.estimate_tokens`) undercounts Claude by ~1.7x on short
prompts (recorded 2026-10-14). Every routed provider tokenises with byte-level BPE or SentencePiece with
byte fallback, so a content token covers at least one UTF-8 byte; chat templates add a bounded number of
tokens per message and per request. The bound is checked against every recorded provider exchange
(tests/unit/test_token_bounds.py); FinOps' `reservation_overrun` alert is the backstop.

The bound over-reserves English text by roughly 4x. That only holds headroom until settlement releases
the unused part; it never changes what is billed.
"""

from collections.abc import Mapping, Sequence

PER_MESSAGE_OVERHEAD_TOKENS = 16
PER_REQUEST_OVERHEAD_TOKENS = 64


def text_token_bound(text: str) -> int:
    return len(text.encode("utf-8"))


def input_token_bound(messages: Sequence[Mapping[str, str]]) -> int:
    """Upper bound on billed input tokens for a chat request carrying `messages`."""
    return PER_REQUEST_OVERHEAD_TOKENS + sum(
        PER_MESSAGE_OVERHEAD_TOKENS + text_token_bound(m["content"]) for m in messages
    )


def bound_for_estimate(estimated_tokens: int, *, messages: int) -> int:
    """Bound for a payload of at most `estimated_tokens` 4-character tokens of single-byte text.

    Sizes the reservation for a CIM package, whose estimate the Context Guard caps. Multi-byte text can
    exceed it; preflight then restores the full eligible context, which is always reserved.
    """
    return PER_REQUEST_OVERHEAD_TOKENS + messages * PER_MESSAGE_OVERHEAD_TOKENS + 4 * estimated_tokens
