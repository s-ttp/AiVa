"""Direct-frontier estimate (the live demo's "AiVa optimized vs Direct frontier" counter).

What the request would have cost sent straight to the pinned frontier model with no AiVa optimization: the
full original context and the served answer's output tokens at the frontier's list price, with no cache
discount. It is arithmetic only and no call is made, so it is `estimated` (less trusted when the tokens it is
priced on are replayed or simulated), never measured, and never mixed with the measured baseline comparison.

It is the savings waterfall's frontier baseline for the same run (`reporting.lines`), so the counter and
Insights agree: the original context is counted on the served attempt's metered token basis
(`metered_original`), not on the gateway's chars/4 estimate. A cache hit avoids the frontier cost of the
answer it reuses. No answer (a blocked or failed run) has no estimate.
"""

from aiva_gateway.core.enums import Provenance
from aiva_gateway.finops.pricing import PriceBook
from aiva_gateway.finops.scopes import least_trusted
from aiva_gateway.pipeline.context import AttemptRecord
from aiva_gateway.pipeline.interfaces import CachedResponse
from aiva_gateway.reporting.lines import metered_original


def direct_frontier_estimate(
    prices: PriceBook,
    *,
    served: AttemptRecord | None,
    context_before: int,
    context_after: int,
    cache_hit: CachedResponse | None = None,
) -> tuple[int | None, Provenance | None]:
    """(micro-USD, label) of the direct-frontier estimate, or (None, None) when no answer was produced."""
    frontier = prices.frontier_model_id
    if cache_hit is not None:
        amount = prices.cost_micros(
            frontier,
            input_tokens=cache_hit.modeled_input_tokens,
            output_tokens=cache_hit.modeled_output_tokens,
        )
        return amount, least_trusted([Provenance.ESTIMATED, cache_hit.provenance])
    if served is None:
        return None, None
    after = context_after or context_before
    original = metered_original(served.input_tokens, context_before, after)
    amount = prices.cost_micros(frontier, input_tokens=original, output_tokens=served.output_tokens)
    return amount, least_trusted([Provenance.ESTIMATED, served.provenance])
