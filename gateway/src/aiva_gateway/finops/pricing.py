"""Cost calculation from pinned rate cards (integer micro-USD, ADR-0006)."""

from collections.abc import Iterable
from dataclasses import dataclass

from aiva_gateway.catalog import Catalog
from aiva_gateway.catalog.models import ModelSpec
from aiva_gateway.core.money import token_cost_micros, usd_to_micros

# Worst-case GPU occupancy per call, reserved up front: the adapter timeout (InferenceRequest default).
GPU_SECONDS_PER_ATTEMPT = 120
_MS_PER_HOUR = 3_600_000


@dataclass(frozen=True)
class Price:
    rate_card_id: str
    input_micros_per_mtok: int
    output_micros_per_mtok: int
    cached_input_micros_per_mtok: int
    gpu_micros_per_hour: int | None

    @property
    def blended_micros_per_mtok(self) -> int:
        return self.input_micros_per_mtok + self.output_micros_per_mtok


def gpu_pools(catalog: Catalog) -> dict[str, str]:
    """Model ID -> GPU pool for models served on a priced GPU endpoint (LLD §12 GPU pool scope)."""
    pools = {}
    for model in catalog.models.models:
        card = catalog.rate_card(model.rate_card_id)
        if card is not None and card.gpu_per_hour is not None:
            pools[model.id] = model.endpoint_name or model.id
    return pools


class PriceBook:
    def __init__(self, catalog: Catalog) -> None:
        self.version = catalog.rate_cards.version
        self.pools = gpu_pools(catalog)
        self.frontier_model_id = catalog.models.frontier_baseline
        # Tool ID -> (rate card ID, micro-USD per billed unit).
        self._tools = {
            card.tool_id: (card.id, usd_to_micros(card.per_unit))
            for card in catalog.rate_cards.tool_rate_cards
        }
        self._prices: dict[str, Price] = {}
        for model in catalog.models.models:
            card = catalog.rate_card(model.rate_card_id)
            if card is None:
                raise ValueError(f"model {model.id} has no rate card")
            self._prices[model.id] = Price(
                rate_card_id=card.id,
                input_micros_per_mtok=usd_to_micros(card.input_per_mtok),
                output_micros_per_mtok=usd_to_micros(card.output_per_mtok),
                cached_input_micros_per_mtok=usd_to_micros(card.cached_input_per_mtok),
                gpu_micros_per_hour=usd_to_micros(card.gpu_per_hour)
                if card.gpu_per_hour is not None
                else None,
            )

    def price(self, model_id: str) -> Price:
        return self._prices[model_id]

    def cost_micros(
        self, model_id: str, *, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0
    ) -> int:
        price = self._prices[model_id]
        uncached = max(0, input_tokens - cached_input_tokens)
        return (
            token_cost_micros(uncached, price.input_micros_per_mtok)
            + token_cost_micros(cached_input_tokens, price.cached_input_micros_per_mtok)
            + token_cost_micros(output_tokens, price.output_micros_per_mtok)
        )

    def tool_rate_card_id(self, tool_id: str) -> str | None:
        """The rate card that prices `tool_id`, or None when the tool is not billed."""
        card = self._tools.get(tool_id)
        return card[0] if card else None

    def tool_cost_micros(self, tool_id: str, units: int) -> int:
        """Cost of `units` billed units of a tool (e.g. Tavily credits); zero for a tool not billed."""
        if units < 0:
            raise ValueError("tool units cannot be negative")
        card = self._tools.get(tool_id)
        return units * card[1] if card else 0

    def worst_case_micros(
        self, model: ModelSpec, *, input_tokens: int, max_output_tokens: int, attempts: int
    ) -> int:
        """Maximum permitted cost for `attempts` calls (initial + retries), reserved up front (LLD §12)."""
        return attempts * self.cost_micros(
            model.id, input_tokens=input_tokens, output_tokens=max_output_tokens
        )

    def gpu_estimate_micros(self, model_id: str, latency_ms: float | None) -> int:
        """Estimated GPU cost of one call: busy time x hourly rate. Zero for models without a GPU rate."""
        rate = self._prices[model_id].gpu_micros_per_hour
        if rate is None or latency_ms is None or latency_ms <= 0:
            return 0
        busy_ms = round(latency_ms)  # durations are measured as floats; money stays integer
        return (busy_ms * rate + _MS_PER_HOUR // 2) // _MS_PER_HOUR

    def gpu_worst_case_micros(self, model_id: str, *, attempts: int) -> int:
        rate = self._prices[model_id].gpu_micros_per_hour
        if rate is None:
            return 0
        busy_ms = attempts * GPU_SECONDS_PER_ATTEMPT * 1000
        return (busy_ms * rate + _MS_PER_HOUR - 1) // _MS_PER_HOUR  # rounded up: it is a ceiling

    def pool_of(self, model_id: str) -> str | None:
        return self.pools.get(model_id)

    def models_in(self, pool: str, model_ids: Iterable[str]) -> list[str]:
        return [m for m in model_ids if self.pools.get(m) == pool]
