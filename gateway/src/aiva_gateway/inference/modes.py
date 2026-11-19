"""Per-run execution mode (ADR-0019).

Each run executes in `replay` or in the gateway's configured mode (`AIVA_INFERENCE_MODE`: live on the
public Space). The request may ask for one (`ChatRequest.execution_mode`):

- no request: a public visitor gets replay; everyone else gets the configured mode;
- `replay`: the per-run replay set (recorded exchanges only, a miss answered by the simulated adapter);
- `live`: the configured mode, only on a live-capable gateway (configured `live` or `record`), and then only
  if Govern's gate allows it (`authorize_execution_mode`: a user needs `chat:live`; an application
  credential runs in the configured mode without a further check). Otherwise the run is refused, never
  silently downgraded.
"""

from dataclasses import dataclass

from aiva_gateway.auth.principal import RunIdentity
from aiva_gateway.auth.run_identity import ExecutionMode, authorize_execution_mode
from aiva_gateway.core.errors import ConflictError

REPLAY = "replay"
LIVE_CAPABLE = frozenset({"live", "record"})
EXECUTION_MODE_UNAVAILABLE = "EXECUTION_MODE_UNAVAILABLE"


@dataclass(frozen=True)
class RunMode:
    # What the run executes in: "replay", or the configured mode ("live", "record", "replay", "simulated").
    mode: str
    # The per-run replay set (a miss is simulated), rather than the configured adapters.
    per_run_replay: bool


def resolve_execution_mode(
    requested: ExecutionMode | None, *, configured: str, identity: RunIdentity
) -> RunMode:
    if requested == REPLAY or (requested is None and identity.is_public_visitor):
        return RunMode(REPLAY, per_run_replay=True)
    if requested is None:
        return RunMode(configured, per_run_replay=False)
    if configured not in LIVE_CAPABLE:
        raise ConflictError(
            "This gateway is not configured for live runs.",
            code=EXECUTION_MODE_UNAVAILABLE,
            remediation="Run in replay mode, or use a gateway configured for live inference.",
        )
    authorize_execution_mode(identity, requested)
    return RunMode(configured, per_run_replay=False)
