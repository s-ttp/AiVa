"""Billed tool calls, e.g. web search (LLD §12, Appendix B): reserved before they run, metered after.

- **Plan.** A stage that will call a billed tool sets `ctx.planned_tool_units` (tool ID -> the worst-case
  billed units of the run) before the budget reservation. `build_envelope` prices them at the tool's rate card
  (`tool_rate_cards`) and adds them to the reservation, in the shared scopes and the public-demo scope only;
  a tool is never a model line.
- **Check.** Before each call the stage calls `check_tool_call`. It refuses (`RESERVATION_INSUFFICIENT`, fail
  closed) when the run holds no reservation or the call would take the run's tool spend past what the
  reservation holds for tools. A refused call is never made.
- **Record.** After the call the stage calls `record_tool_call` with the units the provider billed. The cost
  is integer micro-USD at the rate card; a call billed past the plan is still recorded, because it is real
  spend. The label is the call's: `measured` when the provider reported the units it billed, `estimated` when
  they were inferred, `replayed` or `simulated` when no provider was called.

Settlement, the `tool_call` ledger rows and the response's `cost.tool_micros` follow from `ctx.tool_charges`.
"""

from datetime import UTC, datetime

from aiva_gateway.core.enums import Provenance
from aiva_gateway.core.errors import AivaError
from aiva_gateway.core.ids import new_id
from aiva_gateway.finops.pricing import PriceBook
from aiva_gateway.pipeline.context import RunContext, ToolCharge


def check_tool_call(ctx: RunContext, prices: PriceBook, tool_id: str, units: int) -> int:
    """The call's cost when the reservation covers it; otherwise raise before the call is made."""
    cost = prices.tool_cost_micros(tool_id, units)
    if ctx.reservation is None:
        raise AivaError(
            "A billed tool call needs a budget reservation first.", code="RESERVATION_INSUFFICIENT"
        )
    if ctx.tool_cost_micros + cost > ctx.reserved_tool_micros:
        raise AivaError(
            "The tool call exceeds what the budget reservation holds for tools.",
            code="RESERVATION_INSUFFICIENT",
        )
    return cost


def record_tool_call(
    ctx: RunContext, prices: PriceBook, tool_id: str, units: int, provenance: Provenance
) -> ToolCharge:
    """Meter one tool call the provider billed `units` for."""
    charge = ToolCharge(
        call_id=new_id("tc"),
        tool_id=tool_id,
        units=units,
        cost_micros=prices.tool_cost_micros(tool_id, units),
        provenance=provenance,
        at=datetime.now(UTC),
    )
    ctx.tool_charges.append(charge)
    return charge
