"""Budget scopes, periods and buckets (LLD §12).

A bucket is one scope in one period, e.g. `application|gct-support-chatbot|monthly|2026-10`. Every
reservation is counted in the daily and monthly bucket of each scope it touches, whether or not a budget is
configured there, so a budget created mid-period starts from the true spend. Shared scopes (tenant,
department, application, user, workflow) hold the whole envelope; model scopes hold their model's line and
GPU-pool scopes the line's estimated GPU cost, which is metered separately from API spend. Internet key calls
(ADR-0027) also draw on the tenant's `internet_gpu` scope, which holds their estimated GPU cost across every
GPU pool: a second limit, beside their applications' API budgets, on the in-house endpoints they run.

Buckets live in a money domain (ADR-0008 amendment). `live` buckets count provider spend of runs that can
spend money (live or record inference). Runs that cannot (replay, simulated) reserve and commit in the
`illustrative` domain against the same budgets, so budget demos keep working without draining live caps; a
public visitor's illustrative runs get a domain of their own (`visitor:<pseudonym>`). Illustrative keys keep
the original format (`application|...`); other domains prefix it (`live:application|...`).
"""

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from aiva_gateway.core.enums import Provenance, Role
from aiva_gateway.pipeline.interfaces import Charge, ReservationLine

SHARED_SCOPES = ("tenant", "department", "application", "user", "workflow")
# A public visitor's live runs (ADR-0019) also draw on the tenant's `public_demo` scope, which counts their
# whole cost: API spend plus estimated GPU time, so a sovereign run ($0 token price) still counts.
PUBLIC_DEMO = "public_demo"
# An internet key call's live runs (ADR-0027) also draw on the tenant's `internet_gpu` scope, which counts
# their estimated GPU cost only: the sovereign and CIM endpoints' hosting that no API budget sees.
INTERNET_GPU = "internet_gpu"
CALENDAR_PERIODS = ("daily", "monthly")
LIFETIME = "lifetime"

# Least to most trustworthy (ADR-0006); a total is labelled with its least trustworthy part.
PROVENANCE_TRUST = (
    Provenance.SIMULATED,
    Provenance.REPLAYED,
    Provenance.SYNTHETIC,
    Provenance.ESTIMATED,
    Provenance.MEASURED,
)


# Charges that are money: a provider bill (`measured`) or an estimate of one (`estimated`, e.g. GPU time).
MONEY = frozenset({Provenance.MEASURED, Provenance.ESTIMATED})

# Money domains (ADR-0008 amendment).
LIVE = "live"
ILLUSTRATIVE = "illustrative"
VISITOR = "visitor"
MONEY_MODES = frozenset({"live", "record"})  # inference modes that call paid providers
PUBLIC_VISITOR_ROLE = Role.PUBLIC_VISITOR.value  # ADR-0019


def least_trusted(labels: Iterable[Provenance]) -> Provenance | None:
    present = list(labels)
    return min(present, key=PROVENANCE_TRUST.index) if present else None


def visitor_domain(pseudonym: str) -> str:
    return f"{VISITOR}:{pseudonym}"


def domain_kind(domain: str) -> str:
    """`live`, `illustrative` or `visitor` (a visitor domain without its pseudonym)."""
    return VISITOR if domain.startswith(f"{VISITOR}:") else domain


def configured_domain(inference_mode: str) -> str:
    """The domain of runs in the gateway's configured inference mode: what budget readouts show first."""
    return LIVE if inference_mode in MONEY_MODES else ILLUSTRATIVE


def run_domain(inference_mode: str, *, roles: Collection[str], pseudonym: str | None) -> str:
    """The domain a run reserves and commits in: `live` when it can spend money; otherwise a public
    visitor's own illustrative domain, or the shared illustrative one."""
    if inference_mode in MONEY_MODES:
        return LIVE
    if pseudonym and PUBLIC_VISITOR_ROLE in roles:
        return visitor_domain(pseudonym)
    return ILLUSTRATIVE


def illustrative_label(inference_mode: str) -> Provenance:
    """The label of a non-live run's reservation: `replayed` when it replays recordings, else `simulated`."""
    return Provenance.REPLAYED if inference_mode == "replay" else Provenance.SIMULATED


@dataclass(frozen=True, order=True)
class Bucket:
    scope_type: str
    scope_id: str
    period: str
    period_key: str
    domain: str = ILLUSTRATIVE

    @property
    def key(self) -> str:
        base = "|".join((self.scope_type, self.scope_id, self.period, self.period_key))
        return base if self.domain == ILLUSTRATIVE else f"{self.domain}:{base}"

    @classmethod
    def parse(cls, key: str) -> "Bucket":
        parts = key.split("|")
        if len(parts) != 4:
            raise ValueError(f"malformed bucket key {key!r}")
        domain, _, scope_type = parts[0].rpartition(":")
        return cls(scope_type, parts[1], parts[2], parts[3], domain or ILLUSTRATIVE)

    @property
    def meter(self) -> str:
        """`gpu` buckets (GPU pools, internet key calls) count estimated GPU cost, `total` (public demo) API
        plus GPU, all others API."""
        if self.scope_type in ("gpu_pool", INTERNET_GPU):
            return "gpu"
        return "total" if self.scope_type == PUBLIC_DEMO else "api"


def period_key(period: str, at: datetime) -> str:
    at = at.astimezone(UTC)
    if period == "daily":
        return at.strftime("%Y-%m-%d")
    if period == "monthly":
        return at.strftime("%Y-%m")
    if period == "workflow":
        return LIFETIME
    raise ValueError(f"period {period!r} has no bucket")


def period_bounds(period: str, at: datetime) -> tuple[datetime, datetime] | None:
    """[start, end) of the calendar period containing `at` (UTC), or None for non-calendar periods."""
    at = at.astimezone(UTC)
    if period == "daily":
        start = datetime(at.year, at.month, at.day, tzinfo=UTC)
        return start, start + timedelta(days=1)
    if period == "monthly":
        start = datetime(at.year, at.month, 1, tzinfo=UTC)
        end = datetime(at.year + at.month // 12, at.month % 12 + 1, 1, tzinfo=UTC)
        return start, end
    return None


def application_of(scope_type: str, scope_id: str) -> str | None:
    """The application a scope belongs to: application and request scopes are the application ID, and a
    workflow scope is `<application_id>/<workflow_id>`. Other scopes span applications."""
    if scope_type in ("application", "request"):
        return scope_id
    if scope_type == "workflow" and "/" in scope_id:
        return scope_id.split("/", 1)[0]
    return None


def buckets_for(scope_type: str, scope_id: str, at: datetime, *, domain: str) -> list[Bucket]:
    if scope_type == "workflow":
        return [Bucket(scope_type, scope_id, "workflow", LIFETIME, domain)]
    return [Bucket(scope_type, scope_id, p, period_key(p, at), domain) for p in CALENDAR_PERIODS]


def domain_of(keys: Iterable[str]) -> str:
    """The domain of a reservation's buckets (all of them share one)."""
    return next((Bucket.parse(key).domain for key in keys), ILLUSTRATIVE)


def scope_chain(
    *,
    tenant_id: str,
    department_id: str | None,
    application_id: str,
    principal_pseudonym: str | None,
    workflow_id: str | None,
    public_demo: bool = False,
    internet_gpu: bool = False,
) -> tuple[tuple[str, str], ...]:
    """The hierarchical scopes a run draws on, most senior first. Workflow IDs are per application.
    `public_demo` adds the tenant's public-demo scope, for a public visitor's live run (ADR-0019), and
    `internet_gpu` the tenant's internet GPU scope, for an internet key call's live run (ADR-0027)."""
    chain = [("tenant", tenant_id)]
    if public_demo:
        chain.append((PUBLIC_DEMO, tenant_id))
    if internet_gpu:
        chain.append((INTERNET_GPU, tenant_id))
    if department_id:
        chain.append(("department", department_id))
    chain.append(("application", application_id))
    if principal_pseudonym:
        chain.append(("user", principal_pseudonym))
    if workflow_id:
        chain.append(("workflow", f"{application_id}/{workflow_id}"))
    chain.append(("request", application_id))
    return tuple(chain)


def allocate(
    chain: Iterable[tuple[str, str]],
    amount_micros: int,
    lines: Iterable[ReservationLine],
    at: datetime,
    *,
    domain: str,
) -> dict[str, int]:
    """Reserved amount per bucket key, in the order the scopes are checked."""
    allocations: dict[str, int] = {}

    def add(scope_type: str, scope_id: str, amount: int) -> None:
        for bucket in buckets_for(scope_type, scope_id, at, domain=domain):
            allocations[bucket.key] = allocations.get(bucket.key, 0) + amount

    lines = tuple(lines)
    for scope_type, scope_id in chain:
        if scope_type in SHARED_SCOPES:
            add(scope_type, scope_id, amount_micros)
        elif scope_type == PUBLIC_DEMO:
            add(scope_type, scope_id, amount_micros + sum(line.gpu_micros for line in lines))
        elif scope_type == INTERNET_GPU:
            add(scope_type, scope_id, sum(line.gpu_micros for line in lines))
    for line in lines:
        add("model", line.model_id, line.amount_micros)
        if line.gpu_pool:
            add("gpu_pool", line.gpu_pool, line.gpu_micros)
    return allocations


@dataclass(frozen=True)
class Commitment:
    reserved_micros: int
    committed_micros: int
    provenance: Provenance | None


def commit(
    allocations: Mapping[str, int],
    actual_micros: int,
    charges: Iterable[Charge],
    pools: Mapping[str, str],
    at: datetime,
) -> dict[str, Commitment]:
    """Actual spend per bucket at settlement.

    Shared buckets take the run's actual API cost, model buckets their model's charges and GPU-pool buckets
    the estimated GPU cost of their models. A charge on a model that was never reserved for (preflight
    refuses those calls) is still committed: it is real spend. Live buckets count money only: a `simulated`
    or `replayed` charge commits nothing there.
    """
    domain = domain_of(allocations)
    charge_list = list(charges)
    if domain == LIVE:
        illustrative = sum(c.amount_micros for c in charge_list if c.provenance not in MONEY)
        charge_list = [c for c in charge_list if c.provenance in MONEY]
        actual_micros = max(0, actual_micros - illustrative)
    api: dict[str, int] = {}
    gpu: dict[str, int] = {}
    api_provenance: dict[str, list[Provenance]] = {}
    gpu_provenance: dict[str, list[Provenance]] = {}
    for charge in charge_list:
        if charge.kind == "tool":
            continue  # in the shared scopes (`actual_micros`) only, never in a model or GPU-pool budget
        api[charge.model_id] = api.get(charge.model_id, 0) + charge.amount_micros
        api_provenance.setdefault(charge.model_id, []).append(charge.provenance)
        pool = pools.get(charge.model_id)
        if pool:
            gpu[pool] = gpu.get(pool, 0) + charge.gpu_micros
            # A GPU cost derived from a measured latency is still an estimate (ADR-0006).
            label = least_trusted([charge.provenance, Provenance.ESTIMATED])
            assert label is not None
            gpu_provenance.setdefault(pool, []).append(label)
    shared_provenance = least_trusted(c.provenance for c in charge_list)
    total_provenance = least_trusted([*(c.provenance for c in charge_list), *_flatten(gpu_provenance)])

    keys = dict(allocations)
    for model_id in api:
        for bucket in buckets_for("model", model_id, at, domain=domain):
            keys.setdefault(bucket.key, 0)
    for pool in gpu:
        for bucket in buckets_for("gpu_pool", pool, at, domain=domain):
            keys.setdefault(bucket.key, 0)

    result: dict[str, Commitment] = {}
    for key, reserved in keys.items():
        bucket = Bucket.parse(key)
        if bucket.scope_type == "model":
            amount = api.get(bucket.scope_id, 0)
            label = least_trusted(api_provenance.get(bucket.scope_id, []))
        elif bucket.scope_type == "gpu_pool":
            amount = gpu.get(bucket.scope_id, 0)
            label = least_trusted(gpu_provenance.get(bucket.scope_id, []))
        elif bucket.scope_type == PUBLIC_DEMO:
            amount, label = actual_micros + sum(gpu.values()), total_provenance
        elif bucket.scope_type == INTERNET_GPU:
            amount, label = sum(gpu.values()), least_trusted(_flatten(gpu_provenance))
        else:
            amount, label = actual_micros, shared_provenance
        result[key] = Commitment(reserved, amount, label if amount else None)
    return result


def _flatten(groups: Mapping[str, list[Provenance]]) -> list[Provenance]:
    return [label for labels in groups.values() for label in labels]


def bucket_period_start(bucket: Bucket) -> datetime | None:
    if bucket.period == "daily":
        return datetime.strptime(bucket.period_key, "%Y-%m-%d").replace(tzinfo=UTC)
    if bucket.period == "monthly":
        return datetime.strptime(bucket.period_key, "%Y-%m").replace(tzinfo=UTC)
    return None


# Counters outlive their period long enough for late settlements and reconciliation, then expire. A
# visitor's buckets expire a day after their period (or a day after last use for workflows): the key space
# stays bounded however many visitors there are, and a missing counter is rebuilt from the journal.
_RETENTION = {"daily": timedelta(days=8), "monthly": timedelta(days=40)}
WORKFLOW_RETENTION = timedelta(days=120)
VISITOR_RETENTION = timedelta(days=1)


def bucket_expiry(bucket: Bucket, now: datetime) -> datetime:
    visitor = domain_kind(bucket.domain) == VISITOR
    start = bucket_period_start(bucket)
    bounds = period_bounds(bucket.period, start) if start else None
    if bounds is None:
        return now + (VISITOR_RETENTION if visitor else WORKFLOW_RETENTION)
    return bounds[1] + (VISITOR_RETENTION if visitor else _RETENTION[bucket.period])
