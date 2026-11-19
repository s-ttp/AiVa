"""Maps each registered model to its adapter."""

from aiva_gateway.catalog import Catalog
from aiva_gateway.catalog.models import ModelSpec
from aiva_gateway.pipeline.interfaces import InferenceAdapter


class AdapterRegistry:
    def __init__(
        self, catalog: Catalog, adapters: dict[str, InferenceAdapter], default: InferenceAdapter
    ) -> None:
        self._catalog = catalog
        self._adapters = adapters
        self._default = default

    def for_model(self, model: ModelSpec) -> InferenceAdapter:
        return self._adapters.get(model.id, self._default)

    async def aclose(self) -> None:
        for adapter in self._adapters.values():
            close = getattr(adapter, "aclose", None)
            if close is not None:
                await close()
