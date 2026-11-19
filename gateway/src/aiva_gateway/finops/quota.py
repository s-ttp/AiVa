"""Hierarchical budget reservations (LLD §6 step 10, §12, §17).

`RedisQuotaService` reserves a run's worst-case envelope atomically across every bucket of its scope chain
(tenant, department, application, user, workflow, plus the model and GPU-pool scopes of each model it may
call; a request cap bounds the envelope itself), journals the reservation in Postgres before returning it,
and on settlement commits actual spend per bucket and releases the remainder.

- Atomic: one Lua script checks every capped bucket and applies to all or none, so concurrent requests
  can never take a bucket past its cap.
- Idempotent by key: a repeated reserve with the same idempotency key returns the open reservation instead
  of holding the amount twice; a repeated settle is a no-op.
- Fail closed: an unavailable Redis or Postgres raises `QUOTA_STORE_UNAVAILABLE`; nothing billable runs.
- Rebuildable: a missing bucket (new period, or Redis lost its data) is rebuilt from the Postgres journal
  before any check runs against it.

`SimulatedQuotaService` is the P0 placeholder kept for tests: it enforces nothing and says so (`simulated`).
"""

import asyncio
import json
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aiva_gateway.core.enums import Provenance
from aiva_gateway.core.errors import AivaError, ConflictError, DependencyUnavailableError
from aiva_gateway.core.ids import reservation_id
from aiva_gateway.db.models import AuditLogRow, BudgetReservationRow
from aiva_gateway.finops import alerts
from aiva_gateway.finops.approvals import (
    ALREADY_USED,
    EXCEEDS_APPROVED,
    ApprovedException,
    verify_budget_approval,
)
from aiva_gateway.finops.budgets import BPS, EXHAUSTION_STRICTNESS, BudgetBook, BudgetDef, load_book
from aiva_gateway.finops.ledger import BucketTotals, LedgerReservation, bucket_totals, journal_row
from aiva_gateway.finops.pricing import PriceBook
from aiva_gateway.finops.scopes import (
    INTERNET_GPU,
    LIVE,
    PROVENANCE_TRUST,
    PUBLIC_DEMO,
    Bucket,
    Commitment,
    allocate,
    bucket_expiry,
    bucket_period_start,
    commit,
    domain_of,
    least_trusted,
    period_bounds,
)
from aiva_gateway.finops.scripts import ADJUST, CORRECT, FREE_APPROVAL, HYDRATE, RESERVE, SETTLE
from aiva_gateway.pipeline.interfaces import (
    Charge,
    QuotaDeniedError,
    Reservation,
    ReservationLine,
    ReservationRequest,
)

logger = logging.getLogger(__name__)

_MAX_ROUNDS = 4  # stale budget snapshot / missing buckets resolve within a few rounds
_JOURNAL_WAIT_ROUNDS, _JOURNAL_WAIT_SECONDS = 40, 0.025
SETTLED_RECORD_TTL_SECONDS = 40 * 24 * 3600
APPROVAL_CONSTRAINT = "uq_budget_reservation_approval_id"
_REMEDIATION = "Ask a budget owner to raise the cap or retry in the next period."


def _ms(at: datetime) -> int:
    return int(at.timestamp() * 1000)


def _violated(exc: IntegrityError) -> str | None:
    """The constraint a failed insert violated (asyncpg reports it on the driver error)."""
    return getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None)


def _store_unavailable(message: str | None = None) -> DependencyUnavailableError:
    return DependencyUnavailableError(
        message or "The quota reservation store is unavailable; billable inference was not attempted.",
        code="QUOTA_STORE_UNAVAILABLE",
        remediation="Retry later.",
    )


class QuotaKeys:
    """Redis keys for one tenant. The `{tenant}` hash tag keeps them in one cluster slot."""

    def __init__(self, tenant_id: str) -> None:
        self.prefix = f"aiva:quota:{{{tenant_id}}}"

    @property
    def version(self) -> str:
        return f"{self.prefix}:budget-version"

    @property
    def open(self) -> str:
        return f"{self.prefix}:open"

    @property
    def lock(self) -> str:
        return f"{self.prefix}:reconcile-lock"

    def record(self, idempotency_key: str) -> str:
        return f"{self.prefix}:res:{idempotency_key}"

    def bucket(self, key: str) -> str:
        return f"{self.prefix}:b:{key}"

    def alerted(self, bucket_key: str, kind: str) -> str:
        return f"{self.prefix}:alerted:{kind}:{bucket_key}"

    def approval(self, approval_id: str) -> str:
        return f"{self.prefix}:approval:{approval_id}"


@dataclass(frozen=True)
class BucketState:
    spent: int
    reserved: int
    labels: frozenset[str]
    seq: str | None = None

    @property
    def provenance(self) -> Provenance:
        """Least trustworthy label among the charges counted in `spent`; `measured` when nothing is."""
        present = [Provenance(label) for label in self.labels if label in set(PROVENANCE_TRUST)]
        return least_trusted(present) or Provenance.MEASURED


def _payload(reservation: LedgerReservation) -> str:
    return json.dumps(
        {
            "reservation_id": reservation.reservation_id,
            "run_id": reservation.run_id,
            "tenant_id": reservation.tenant_id,
            "idempotency_key": reservation.idempotency_key,
            "amount_micros": reservation.amount_micros,
            "scope_chain": [list(scope) for scope in reservation.scope_chain],
            "lines": [asdict(line) for line in reservation.lines],
            "created_at": reservation.created_at.isoformat(),
            "allocations": [list(item) for item in reservation.allocations],
            "provenance": reservation.provenance.value,
            "approval_id": reservation.approval_id,
        },
        separators=(",", ":"),
    )


def reservation_from_payload(payload: str) -> LedgerReservation:
    data = json.loads(payload)
    return LedgerReservation(
        reservation_id=data["reservation_id"],
        amount_micros=int(data["amount_micros"]),
        scope_chain=tuple((str(s[0]), str(s[1])) for s in data["scope_chain"]),
        provenance=Provenance(data["provenance"]),
        lines=tuple(ReservationLine(**line) for line in data["lines"]),
        tenant_id=data["tenant_id"],
        run_id=data["run_id"],
        idempotency_key=data["idempotency_key"],
        created_at=datetime.fromisoformat(data["created_at"]),
        allocations=tuple((str(k), int(v)) for k, v in data["allocations"]),
        approval_id=data.get("approval_id"),
    )


@dataclass
class _Attempt:
    """Per-reserve state: the approved exception in force, why one was refused, and what it was used for."""

    exception: ApprovedException | None = None
    refusal: str | None = None
    exceeded: tuple[tuple[BudgetDef, Bucket], ...] = ()

    def refusal_note(self) -> str:
        return f" A budget approval was presented but not applied: {self.refusal}." if self.refusal else ""

    def headroom(self, budget: BudgetDef | None) -> int:
        if self.exception is None or budget is None or budget.id not in self.exception.budget_ids:
            return 0
        return self.exception.allowance_micros

    def fit(self, needs: Iterable[tuple[BudgetDef | None, int]]) -> None:
        """Withdraw the exception when a budget it names needs more for this request than was approved."""
        if any(self.headroom(budget) and amount > self.headroom(budget) for budget, amount in needs):
            self.exception, self.refusal = None, EXCEEDS_APPROVED


class RedisQuotaService:
    def __init__(
        self,
        redis: Redis,
        sessionmaker: async_sessionmaker[AsyncSession],
        prices: PriceBook,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.redis = redis
        self.sessionmaker = sessionmaker
        self.prices = prices
        self.clock = clock
        self._books: dict[str, BudgetBook] = {}
        self._reserve = redis.register_script(RESERVE)
        self._settle = redis.register_script(SETTLE)
        self._hydrate = redis.register_script(HYDRATE)
        self._adjust = redis.register_script(ADJUST)
        self._correct = redis.register_script(CORRECT)
        self._free_approval = redis.register_script(FREE_APPROVAL)

    # --- Budget definitions ------------------------------------------------------------------------

    async def book(self, tenant_id: str, *, refresh: bool = False) -> BudgetBook:
        """The tenant's budgets. A fresh load publishes its version so every process converges on it."""
        cached = self._books.get(tenant_id)
        if cached is not None and not refresh:
            return cached
        async with self.sessionmaker() as session:
            book = await load_book(session, tenant_id)
        await self.redis.set(QuotaKeys(tenant_id).version, book.version, nx=not refresh)
        self._books[tenant_id] = book
        return book

    async def publish_budgets(self, tenant_id: str) -> BudgetBook:
        """Call after budgets change in Postgres: reloads and makes every process reload on next use."""
        return await self.book(tenant_id, refresh=True)

    # --- Reserve -----------------------------------------------------------------------------------

    async def reserve(self, request: ReservationRequest) -> Reservation:
        now = self.clock()
        allocations = allocate(
            request.scope_chain, request.amount_micros, request.lines, now, domain=request.domain
        )
        reservation = LedgerReservation(
            reservation_id=reservation_id(),
            amount_micros=request.amount_micros,
            scope_chain=request.scope_chain,
            # The hold in the live store is exact; outside the live domain it is illustrative (ADR-0006).
            provenance=Provenance.MEASURED if request.domain == LIVE else request.illustrative_label,
            lines=request.lines,
            tenant_id=request.tenant_id,
            run_id=request.run_id,
            idempotency_key=request.idempotency_key,
            created_at=now,
            allocations=tuple(allocations.items()),
        )
        attempt = _Attempt()
        try:
            if request.approval_id:
                attempt = await self._approval(request, now)
            existing = await self._reserve_atomically(reservation, attempt)
        except (RedisError, SQLAlchemyError, OSError) as exc:
            raise _store_unavailable() from exc
        if existing is not None:
            return existing
        if attempt.exception is not None:
            reservation = replace(reservation, approval_id=attempt.exception.approval_id)
        try:
            async with self.sessionmaker() as session, session.begin():
                session.add(journal_row(reservation, pricing_version=self.prices.version))
                if attempt.exceeded:
                    session.add(self._exception_audit(reservation, attempt))
        except IntegrityError as exc:
            await self._release_quietly(reservation)
            if _violated(exc) != APPROVAL_CONSTRAINT:
                raise _store_unavailable(
                    "The budget ledger is unavailable; billable inference was not attempted."
                ) from exc
            # The durable record shows this approval already funded a reservation (ADR-0014).
            raise ConflictError(
                "This budget approval has already been used; it funds one request only.",
                code="BUDGET_APPROVAL_USED",
                remediation="Submit the request again to create a new approval request.",
            ) from exc
        except (SQLAlchemyError, OSError) as exc:
            # Without a durable record the hold cannot be reconciled: undo it and fail closed. The approval
            # funded nothing, so it is freed for the retry Govern allows after this retryable failure.
            await self._release_quietly(reservation, free_approval=True)
            raise _store_unavailable(
                "The budget ledger is unavailable; billable inference was not attempted."
            ) from exc
        if attempt.exceeded and attempt.exception is not None:
            await alerts.record_alerts(
                self.sessionmaker,
                [
                    alerts.approved_exception_alert(
                        budget,
                        bucket,
                        amount=dict(reservation.allocations).get(bucket.key, reservation.amount_micros),
                        approval_id=attempt.exception.approval_id,
                        reservation_id=reservation.reservation_id,
                        run_id=reservation.run_id,
                    )
                    for budget, bucket in attempt.exceeded
                ],
            )
        return reservation

    async def _approval(self, request: ReservationRequest, now: datetime) -> _Attempt:
        """Verify the presented budget approval; an approval that fails any rule is simply not applied."""
        assert request.approval_id is not None
        application = next((sid for scope, sid in request.scope_chain if scope == "application"), None)
        async with self.sessionmaker() as session:
            verdict = await verify_budget_approval(
                session,
                approval_id=request.approval_id,
                tenant_id=request.tenant_id,
                application_id=application,
                run_id=request.run_id,
                now=now,
            )
        if isinstance(verdict, str):
            return _Attempt(refusal=verdict)
        return _Attempt(exception=verdict)

    def _exception_audit(self, reservation: LedgerReservation, attempt: _Attempt) -> AuditLogRow:
        assert attempt.exception is not None
        amounts = dict(reservation.allocations)
        return AuditLogRow(
            tenant_id=reservation.tenant_id,
            actor_kind="system",
            actor_id="finops",
            action="budget.approved_exception",
            target_type="budget_reservation",
            target_id=reservation.reservation_id,
            details={
                "approval_id": attempt.exception.approval_id,
                "run_id": reservation.run_id,
                "budget_ids": sorted({budget.id for budget, _ in attempt.exceeded}),
                "buckets": {
                    bucket.key: amounts.get(bucket.key, reservation.amount_micros)
                    for _, bucket in attempt.exceeded
                },
                "approved_amount_micros": attempt.exception.allowance_micros,
            },
        )

    async def _reserve_atomically(
        self, reservation: LedgerReservation, attempt: _Attempt
    ) -> LedgerReservation | None:
        tenant = reservation.tenant_id
        keys = QuotaKeys(tenant)
        buckets = [Bucket.parse(key) for key, _ in reservation.allocations]
        book = await self.book(tenant)
        _require_public_demo_budget(book, buckets)
        _require_internet_gpu_budget(book, buckets)
        for _ in range(_MAX_ROUNDS):
            budgets = [book.for_bucket(bucket) for bucket in buckets]
            request_budget = self._request_budget(book, reservation)
            attempt.fit(
                [
                    *zip(budgets, (amount for _, amount in reservation.allocations), strict=True),
                    (request_budget, reservation.amount_micros),
                ]
            )
            request_exceeded = await self._check_request_cap(request_budget, reservation, attempt)
            approval = attempt.exception.approval_id if attempt.exception else ""
            args: list[str] = [
                book.version,
                reservation.reservation_id,
                str(_ms(reservation.created_at)),
                _payload(replace(reservation, approval_id=approval or None)),
                approval,
                str(SETTLED_RECORD_TTL_SECONDS),
            ]
            for budget, (_, amount) in zip(budgets, reservation.allocations, strict=True):
                args += [
                    str(amount),
                    str(budget.cap_micros) if budget else "-1",
                    str(attempt.headroom(budget)),
                ]
            reply = await self._reserve(
                keys=[
                    keys.version,
                    keys.record(reservation.idempotency_key),
                    keys.open,
                    keys.approval(approval or "-"),
                ]
                + [keys.bucket(b.key) for b in buckets],
                args=args,
            )
            match reply[0]:
                case "ok":
                    exceeded = [(budgets[int(i) - 1], buckets[int(i) - 1]) for i in reply[1:]]
                    attempt.exceeded = tuple(
                        (budget, bucket) for budget, bucket in [*request_exceeded, *exceeded] if budget
                    )
                    return None
                case "stale":
                    book = await self.book(tenant, refresh=True)
                case "missing":
                    await self.hydrate(tenant, [buckets[int(i) - 1] for i in reply[1:]])
                case "existing":
                    return await self._existing(reply)
                case "approval_used":
                    # Single use: it already funded a reservation. Re-evaluate against the ordinary caps.
                    attempt.exception, attempt.refusal = None, ALREADY_USED
                case "denied":
                    await self._deny(book, buckets, list(reply[1:]), reservation, attempt)
        raise _store_unavailable("The quota store is busy; billable inference was not attempted.")

    async def _existing(self, reply: Sequence[str]) -> LedgerReservation:
        """The open reservation for a repeated key, once its first caller has journaled it."""
        _, _, state, payload = reply
        if state != "open" or not payload:
            raise AivaError(
                "This request's budget reservation was already settled; it cannot fund another run.",
                code="RESERVATION_ALREADY_SETTLED",
                remediation="Retry with a new Idempotency-Key.",
            )
        reservation = reservation_from_payload(payload)
        for _ in range(_JOURNAL_WAIT_ROUNDS):
            async with self.sessionmaker() as session:
                if await session.get(BudgetReservationRow, reservation.reservation_id) is not None:
                    return reservation
            await asyncio.sleep(_JOURNAL_WAIT_SECONDS)
        # The first caller has not journaled it (or failed and is undoing it): never fund a call on it.
        raise _store_unavailable("The budget reservation is still being recorded; retry shortly.")

    @staticmethod
    def _request_budget(book: BudgetBook, reservation: LedgerReservation) -> BudgetDef | None:
        scope_id = next((sid for scope, sid in reservation.scope_chain if scope == "request"), None)
        return book.for_request(scope_id) if scope_id else None

    async def _check_request_cap(
        self, budget: BudgetDef | None, reservation: LedgerReservation, attempt: _Attempt
    ) -> list[tuple[BudgetDef | None, Bucket]]:
        """Stateless per-request ceiling. Returns the request cap when an approved exception lifted it."""
        amount = reservation.amount_micros
        if budget is None or amount <= 0 or amount <= budget.cap_micros:
            return []
        scope_id = next(sid for scope, sid in reservation.scope_chain if scope == "request")
        day = self.clock().strftime("%Y-%m-%d")
        bucket = Bucket("request", scope_id, "request", day, domain_of(k for k, _ in reservation.allocations))
        if amount <= budget.cap_micros + attempt.headroom(budget):
            return [(budget, bucket)]
        await self._alert_once(
            bucket,
            alerts.HARD_CAP,
            alerts.denial_alert(budget, bucket, 0, amount, Provenance.ESTIMATED, run_id=reservation.run_id),
            ttl_seconds=2 * 24 * 3600,
        )
        raise QuotaDeniedError(
            "The per-request budget does not cover this request's worst-case cost." + attempt.refusal_note(),
            rule_id=budget.id,
            on_exhaustion=budget.on_exhaustion,
            scope_type="request",
            scope_id=scope_id,
            amount_micros=amount,
        )

    async def _deny(
        self,
        book: BudgetBook,
        buckets: list[Bucket],
        flat: list[str],
        reservation: LedgerReservation,
        attempt: _Attempt,
    ) -> None:
        amounts = dict(reservation.allocations)
        exhausted: list[tuple[BudgetDef, Bucket, int, int]] = []
        for i in range(0, len(flat), 3):
            bucket = buckets[int(flat[i]) - 1]
            budget = book.for_bucket(bucket)
            assert budget is not None, "only capped buckets can deny"
            exhausted.append((budget, bucket, int(flat[i + 1]), int(flat[i + 2])))
        for budget, bucket, spent, _ in exhausted:
            draft = alerts.denial_alert(
                budget, bucket, spent, amounts[bucket.key], Provenance.ESTIMATED, run_id=reservation.run_id
            )
            await self._alert_once(bucket, f"{alerts.HARD_CAP}:{BPS}", draft)
        budget, bucket, spent, reserved = max(
            exhausted, key=lambda e: EXHAUSTION_STRICTNESS[e[0].on_exhaustion]
        )
        start = bucket_period_start(bucket)
        # A $0 cap is a budget switched off: its headroom never returns, so there is no reset time.
        bounds = period_bounds(bucket.period, start) if start and budget.cap_micros > 0 else None
        raise QuotaDeniedError(
            f"The {bucket.period} {bucket.scope_type.replace('_', ' ')} budget does not cover this request."
            + attempt.refusal_note(),
            rule_id=budget.id,
            on_exhaustion=budget.on_exhaustion,
            scope_type=bucket.scope_type,
            scope_id=bucket.scope_id,
            amount_micros=amounts[bucket.key],
            resets_at=bounds[1] if bounds else None,
            # An approval lifts the cap by this request's share only, so past the cap it can never admit it.
            already_exhausted=spent + reserved > budget.cap_micros,
        )

    # --- Settle and release --------------------------------------------------------------------------

    async def settle(
        self, reservation: Reservation, actual_micros: int, *, charges: tuple[Charge, ...] = ()
    ) -> int:
        if not isinstance(reservation, LedgerReservation):
            raise TypeError("RedisQuotaService can only settle its own reservations")
        commitments = commit(
            dict(reservation.allocations), actual_micros, charges, self.prices.pools, reservation.created_at
        )
        released = max(0, reservation.amount_micros - actual_micros)
        try:
            outcome = await self.apply_settlement(reservation, commitments, released=released)
        except (RedisError, SQLAlchemyError, OSError) as exc:
            raise _store_unavailable("The quota store is unavailable; the reservation stays open.") from exc
        if outcome is None:
            return released
        try:
            drafts = await self._crossing_alerts(reservation.tenant_id, outcome, run_id=reservation.run_id)
        except (RedisError, SQLAlchemyError, OSError):
            logger.warning("Threshold alerts skipped for %s", reservation.reservation_id, exc_info=True)
            drafts = []
        if actual_micros > reservation.amount_micros:
            overrun = actual_micros - reservation.amount_micros
            drafts.append(
                alerts.AlertDraft(
                    tenant_id=reservation.tenant_id,
                    kind=alerts.RESERVATION_OVERRUN,
                    message=f"Actual spend exceeded its reservation by {alerts.usd(overrun)}; the worst-case "
                    "estimate was too low and the excess was committed.",
                    provenance=least_trusted(c.provenance for c in charges) or Provenance.MEASURED,
                    amount_micros=overrun,
                    reservation_id=reservation.reservation_id,
                    run_id=reservation.run_id,
                    domain=domain_of(k for k, _ in reservation.allocations),
                )
            )
        await alerts.record_alerts(self.sessionmaker, drafts)
        return released

    async def apply_settlement(
        self,
        reservation: LedgerReservation,
        commitments: Mapping[str, Commitment],
        *,
        released: int,
        state: str = "settled",
    ) -> dict[str, tuple[int, int]] | None:
        """Commit per-bucket spend and release holds. Returns spent before/after per bucket, or None if
        the reservation was already settled or released (idempotent)."""
        tenant = reservation.tenant_id
        keys = QuotaKeys(tenant)
        order = list(commitments)
        args = [
            reservation.reservation_id,
            state,
            str(SETTLED_RECORD_TTL_SECONDS),
            json.dumps({"released": released}),
        ]
        for key in order:
            c = commitments[key]
            args += [
                str(-c.reserved_micros),
                str(c.committed_micros),
                c.provenance.value if c.provenance else "",
            ]
        for _ in range(_MAX_ROUNDS):
            reply = await self._settle(
                keys=[keys.record(reservation.idempotency_key), keys.open] + [keys.bucket(k) for k in order],
                args=args,
            )
            if reply[0] == "done":
                return None
            if reply[0] == "missing":
                await self.hydrate(tenant, [Bucket.parse(order[int(i) - 1]) for i in reply[1:]])
                continue
            values = reply[1:]
            return {key: (int(values[2 * i]), int(values[2 * i + 1])) for i, key in enumerate(order)}
        raise _store_unavailable("The quota store is busy; the reservation stays open.")

    async def release(self, reservation: LedgerReservation, *, state: str = "released") -> None:
        """Cancel a reservation without spend (it never funded a call)."""
        commitments = commit(dict(reservation.allocations), 0, (), self.prices.pools, reservation.created_at)
        await self.apply_settlement(reservation, commitments, released=reservation.amount_micros, state=state)

    async def _release_quietly(self, reservation: LedgerReservation, *, free_approval: bool = False) -> None:
        try:
            await self.release(reservation)
            if free_approval and reservation.approval_id:
                keys = QuotaKeys(reservation.tenant_id)
                await self._free_approval(
                    keys=[keys.approval(reservation.approval_id)], args=[reservation.reservation_id]
                )
        except (RedisError, OSError, AivaError):
            # Left open in Redis only: reconciliation releases Redis-side orphans.
            logger.warning("Could not release reservation %s", reservation.reservation_id, exc_info=True)

    # --- Counters ------------------------------------------------------------------------------------

    async def hydrate(self, tenant_id: str, buckets: Sequence[Bucket]) -> None:
        """Create absent buckets from the durable journal (settled spend + adjustments, open holds)."""
        if not buckets:
            return
        now = self.clock()
        async with self.sessionmaker() as session:
            totals = await bucket_totals(session, tenant_id, [b.key for b in buckets])
        keys = QuotaKeys(tenant_id)
        args = [str(_ms(now))]
        for bucket in buckets:
            total = totals.get(bucket.key, BucketTotals())
            expiry = int(bucket_expiry(bucket, now).timestamp())
            args += [str(total.spent), str(total.reserved), str(expiry), ",".join(sorted(total.labels))]
        await self._hydrate(keys=[keys.bucket(b.key) for b in buckets], args=args)

    async def bucket_states(self, tenant_id: str, buckets: Sequence[Bucket]) -> dict[str, BucketState]:
        """Live counters for the given buckets, rebuilt from the journal where absent."""
        keys = QuotaKeys(tenant_id)
        states: dict[str, BucketState] = {}
        for _ in range(2):
            pipe = self.redis.pipeline(transaction=False)
            for bucket in buckets:
                pipe.hgetall(keys.bucket(bucket.key))
            replies = await pipe.execute()
            missing = []
            for bucket, fields in zip(buckets, replies, strict=True):
                if not fields:
                    missing.append(bucket)
                    continue
                states[bucket.key] = BucketState(
                    spent=int(fields.get("spent", 0)),
                    reserved=int(fields.get("reserved", 0)),
                    labels=frozenset(f[5:] for f in fields if f.startswith("prov:")),
                    seq=fields.get("seq"),
                )
            if not missing:
                break
            await self.hydrate(tenant_id, missing)
        return states

    async def apply_adjustments(
        self, tenant_id: str, deltas: Mapping[str, int], *, provenance: Provenance
    ) -> dict[str, tuple[int, int]]:
        """Add signed corrections to spent on existing buckets (absent ones rebuild from the journal)."""
        if not deltas:
            return {}
        keys = QuotaKeys(tenant_id)
        order = list(deltas)
        reply = await self._adjust(keys=[keys.bucket(k) for k in order], args=[str(deltas[k]) for k in order])
        moved = {
            key: (int(reply[2 * i]), int(reply[2 * i + 1]))
            for i, key in enumerate(order)
            if reply[2 * i] != ""
        }
        drafts = await self._crossing_alerts(tenant_id, moved, run_id=None, provenance=provenance)
        await alerts.record_alerts(self.sessionmaker, drafts)
        return moved

    async def correct(self, tenant_id: str, bucket: Bucket, *, seq: str, spent: int) -> str:
        reply = await self._correct(keys=[QuotaKeys(tenant_id).bucket(bucket.key)], args=[seq, str(spent)])
        return str(reply[0])

    async def acquire_lock(self, tenant_id: str, owner: str, ttl_seconds: int) -> bool:
        return bool(await self.redis.set(QuotaKeys(tenant_id).lock, owner, nx=True, ex=ttl_seconds))

    async def release_lock(self, tenant_id: str, owner: str) -> None:
        key = QuotaKeys(tenant_id).lock
        if await self.redis.get(key) == owner:
            await self.redis.delete(key)

    # --- Alerts --------------------------------------------------------------------------------------

    async def _crossing_alerts(
        self,
        tenant_id: str,
        moved: Mapping[str, tuple[int, int]],
        *,
        run_id: str | None,
        provenance: Provenance | None = None,
    ) -> list[alerts.AlertDraft]:
        book = await self.book(tenant_id)
        drafts = []
        for key, (before, after) in moved.items():
            bucket = Bucket.parse(key)
            budget = book.for_bucket(bucket)
            if budget is None or budget.cap_micros == 0:
                continue
            for bps in budget.crossings(before, after):
                label = provenance or (await self.bucket_states(tenant_id, [bucket]))[key].provenance
                draft = alerts.threshold_alert(budget, bucket, bps, after, label, run_id=run_id)
                if await self._claim(tenant_id, bucket, f"{draft.kind}:{bps}"):
                    drafts.append(draft)
        return drafts

    async def _claim(self, tenant_id: str, bucket: Bucket, kind: str, ttl_seconds: int | None = None) -> bool:
        """Dedupe: an alert fires once per bucket (so once per period) and kind."""
        now = self.clock()
        ttl = ttl_seconds or max(60, int((bucket_expiry(bucket, now) - now).total_seconds()))
        try:
            key = QuotaKeys(tenant_id).alerted(bucket.key, kind)
            return bool(await self.redis.set(key, "1", nx=True, ex=ttl))
        except RedisError:
            return True

    async def _alert_once(
        self, bucket: Bucket, kind: str, draft: alerts.AlertDraft, *, ttl_seconds: int | None = None
    ) -> None:
        if await self._claim(draft.tenant_id, bucket, kind, ttl_seconds):
            await alerts.record_alerts(self.sessionmaker, [draft])


def _require_public_demo_budget(book: BudgetBook, buckets: Sequence[Bucket]) -> None:
    """A public visitor's live run needs a configured public-demo budget: without one there is no cap on
    public spend, so the run is refused (ADR-0019) rather than left uncapped."""
    demo = [bucket for bucket in buckets if bucket.scope_type == PUBLIC_DEMO]
    if demo and not any(book.for_bucket(bucket) for bucket in demo):
        raise QuotaDeniedError(
            "No public-demo budget is configured, so public live runs are disabled.",
            rule_id=f"{PUBLIC_DEMO}:unconfigured",
            scope_type=PUBLIC_DEMO,
            scope_id=demo[0].scope_id,
        )


def _require_internet_gpu_budget(book: BudgetBook, buckets: Sequence[Bucket]) -> None:
    """An internet key call's live run needs a configured internet GPU budget (ADR-0027): without one the
    in-house endpoints' hosting it could run up has no cap, so the run is refused, not left uncapped."""
    gpu = [bucket for bucket in buckets if bucket.scope_type == INTERNET_GPU]
    if gpu and not any(book.for_bucket(bucket) for bucket in gpu):
        raise QuotaDeniedError(
            "No GPU budget for internet key calls is configured, so they are refused.",
            rule_id=f"{INTERNET_GPU}:unconfigured",
            scope_type=INTERNET_GPU,
            scope_id=gpu[0].scope_id,
        )


async def invalidate_budgets(redis: Redis, tenant_id: str) -> None:
    """After budgets change outside the API (seeding): every process reloads them on next use."""
    await redis.delete(QuotaKeys(tenant_id).version)


class SimulatedQuotaService:
    """P0 placeholder: reservations always succeed and are labelled `simulated` (nothing is enforced)."""

    def __init__(self, *, available: bool = True) -> None:
        self.available = available

    def _check(self) -> None:
        if not self.available:
            raise _store_unavailable()

    async def reserve(self, request: ReservationRequest) -> Reservation:
        self._check()
        return Reservation(
            reservation_id=reservation_id(),
            amount_micros=request.amount_micros,
            scope_chain=request.scope_chain,
            provenance=Provenance.SIMULATED,
            lines=request.lines,
        )

    async def settle(
        self, reservation: Reservation, actual_micros: int, *, charges: tuple[Charge, ...] = ()
    ) -> int:
        self._check()
        return max(0, reservation.amount_micros - actual_micros)
