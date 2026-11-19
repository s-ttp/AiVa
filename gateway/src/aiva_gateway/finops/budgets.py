"""Budget definitions and the arithmetic of caps, thresholds and forecasts (LLD §12, §21.9, §22.6).

Definitions live in Postgres (`budget`, seeded from config/tenant.yaml, edited via /v1/admin/budgets). A
`BudgetBook` is an immutable snapshot of one tenant's budgets with a content hash; the reservation script
refuses to run against a snapshot whose hash differs from the one published in Redis, so a cap change is
enforced by every gateway process from the next reservation on.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aiva_gateway.core.canonical import sha256_hex
from aiva_gateway.db.models import BudgetRow
from aiva_gateway.finops.scopes import Bucket, period_bounds

BPS = 10_000  # thresholds are held as integer basis points; 10_000 = the hard cap
DEFAULT_SCOPE = "default"  # a template applying to every scope of its type without its own budget

# On insufficient balance the strictest exhaustion action among the exhausted budgets applies.
EXHAUSTION_STRICTNESS = {"block": 2, "approval_required": 1, "lower_cost_route": 0}

VALID_PERIODS = {
    "tenant": {"daily", "monthly"},
    "public_demo": {"daily", "monthly"},
    "internet_gpu": {"daily", "monthly"},
    "department": {"daily", "monthly"},
    "application": {"daily", "monthly"},
    "user": {"daily", "monthly"},
    "model": {"daily", "monthly"},
    "gpu_pool": {"daily", "monthly"},
    "workflow": {"workflow"},
    "request": {"request"},
}


def to_bps(ratio: float | Decimal | str) -> int:
    return int((Decimal(str(ratio)) * BPS).to_integral_value(rounding=ROUND_HALF_UP))


def from_bps(bps: int) -> float:
    return float(Decimal(bps) / BPS)


def budget_id(scope_type: str, scope_id: str, period: str) -> str:
    readable = f"bud-{scope_type}-{scope_id}-{period}"
    if len(readable) <= 64 and "/" not in scope_id:
        return readable
    return f"bud-{scope_type}-{sha256_hex([scope_type, scope_id, period])[:24]}"


@dataclass(frozen=True)
class BudgetDef:
    id: str
    tenant_id: str
    scope_type: str
    scope_id: str
    period: str
    cap_micros: int
    thresholds_bps: tuple[int, ...]
    on_exhaustion: str

    @property
    def is_template(self) -> bool:
        return self.scope_id == DEFAULT_SCOPE

    def threshold_micros(self, bps: int) -> int:
        return self.cap_micros * bps // BPS

    def crossings(self, before: int, after: int) -> list[int]:
        """Thresholds (and the hard cap, as 10_000 bps) that spend moved across, upwards."""
        return [bps for bps in (*self.thresholds_bps, BPS) if before < self.threshold_micros(bps) <= after]

    def crossed(self, spent: int) -> list[int]:
        return [bps for bps in (*self.thresholds_bps, BPS) if spent >= self.threshold_micros(bps)]


@dataclass(frozen=True)
class BudgetBook:
    tenant_id: str
    version: str
    budgets: tuple[BudgetDef, ...]
    _index: dict[tuple[str, str, str], BudgetDef] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        self._index.update({(b.scope_type, b.scope_id, b.period): b for b in self.budgets})

    def find(self, scope_type: str, scope_id: str, period: str) -> BudgetDef | None:
        return self._index.get((scope_type, scope_id, period)) or self._index.get(
            (scope_type, DEFAULT_SCOPE, period)
        )

    def for_bucket(self, bucket: Bucket) -> BudgetDef | None:
        return self.find(bucket.scope_type, bucket.scope_id, bucket.period)

    def for_request(self, scope_id: str) -> BudgetDef | None:
        return self.find("request", scope_id, "request")


def to_def(row: BudgetRow) -> BudgetDef:
    return BudgetDef(
        id=row.id,
        tenant_id=row.tenant_id,
        scope_type=row.scope_type,
        scope_id=row.scope_id,
        period=row.period,
        cap_micros=row.cap_micros,
        thresholds_bps=tuple(sorted({to_bps(t) for t in row.alert_thresholds})),
        on_exhaustion=row.on_exhaustion,
    )


def make_book(tenant_id: str, budgets: Iterable[BudgetDef]) -> BudgetBook:
    ordered = tuple(sorted(budgets, key=lambda b: b.id))
    version = sha256_hex(
        [
            [b.id, b.scope_type, b.scope_id, b.period, b.cap_micros, list(b.thresholds_bps), b.on_exhaustion]
            for b in ordered
        ]
    )[:20]
    return BudgetBook(tenant_id=tenant_id, version=version, budgets=ordered)


async def load_book(session: AsyncSession, tenant_id: str) -> BudgetBook:
    rows = (await session.execute(select(BudgetRow).where(BudgetRow.tenant_id == tenant_id))).scalars()
    return make_book(tenant_id, (to_def(row) for row in rows))


def forecast_micros(period: str, spent: int, now: datetime) -> int | None:
    """Linear run-rate projection of spend to the end of a calendar period. Always an estimate."""
    bounds = period_bounds(period, now)
    if bounds is None:
        return None
    start, end = bounds
    elapsed = max(1, int((now - start).total_seconds()))
    total = int((end - start).total_seconds())
    return spent * total // elapsed
