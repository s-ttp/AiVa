"""Reconciliation job: reservations <-> usage ledger <-> live counters (LLD §12, §21.2).

The attempt-level usage ledger is authoritative for spend. Each run of the job:

1. settles open reservations whose run finished but whose settlement never reached Redis, from the ledger;
   releases those whose run never recorded (an orphan) and raises an alert to review provider billing;
2. releases holds that exist only in Redis (the journal write failed and so did the undo);
3. compares every settled reservation's committed spend with its ledger rows, including `adjustment` rows
   appended later, and writes the difference per bucket as new append-only `budget_adjustment` rows,
   applying the same deltas to the live counters;
4. corrects counter drift on budgeted buckets, but only while a bucket is quiescent (nothing reserved and
   unchanged while the ledger was read), so it can never race a live reservation;
5. records the run and its findings in `reconciliation_run`.
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import ARRAY, ColumnElement, String, bindparam, cast, exists, func, literal, or_, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aiva_gateway.catalog import Catalog
from aiva_gateway.core.enums import ChargeType, Provenance
from aiva_gateway.core.errors import ConflictError
from aiva_gateway.core.ids import new_id
from aiva_gateway.db import models as db
from aiva_gateway.finops import alerts
from aiva_gateway.finops.ledger import (
    LedgerReservation,
    bucket_totals,
    committed_from_row,
    reservation_from_row,
)
from aiva_gateway.finops.quota import QuotaKeys, RedisQuotaService, reservation_from_payload
from aiva_gateway.finops.scopes import (
    ILLUSTRATIVE,
    LIVE,
    Bucket,
    Commitment,
    buckets_for,
    commit,
    domain_of,
    least_trusted,
)
from aiva_gateway.pipeline.interfaces import Charge

logger = logging.getLogger(__name__)


def _store_held() -> ColumnElement[bool]:
    """Reservations the quota store made, which it journals with their allocations. The P0 placeholder's
    records are journaled after the run with none and hold nothing to reconcile. (Their `simulated` label
    no longer tells them apart: illustrative reservations carry it too, ADR-0008 amendment.)"""
    return db.BudgetReservationRow.allocations != cast(literal("{}"), JSONB)


LOCK_TTL_SECONDS = 300


@dataclass
class Finding:
    kind: str
    detail: str
    reservation_id: str | None = None
    bucket: str | None = None
    delta_micros: int | None = None


@dataclass
class ReconciliationResult:
    reconciliation_id: str
    tenant_id: str
    triggered_by: str
    started_at: datetime
    status: str = "running"
    completed_at: datetime | None = None
    reservations_checked: int = 0
    settled: int = 0
    expired: int = 0
    adjustments: int = 0
    counters_corrected: int = 0
    findings: list[Finding] = field(default_factory=list)
    error: str | None = None

    def row(self) -> db.ReconciliationRunRow:
        return db.ReconciliationRunRow(
            reconciliation_id=self.reconciliation_id,
            tenant_id=self.tenant_id,
            triggered_by=self.triggered_by,
            status=self.status,
            reservations_checked=self.reservations_checked,
            settled=self.settled,
            expired=self.expired,
            adjustments=self.adjustments,
            counters_corrected=self.counters_corrected,
            findings=[asdict(f) for f in self.findings],
            error=self.error,
            started_at=self.started_at,
            completed_at=self.completed_at,
        )


@dataclass
class LedgerSpend:
    """A reservation's spend per the usage ledger."""

    api_micros: int = 0
    charges: list[Charge] = field(default_factory=list)


async def ledger_spend(session: AsyncSession, reservation_ids: Sequence[str]) -> dict[str, LedgerSpend]:
    if not reservation_ids:
        return {}
    statement = (
        select(
            db.UsageLedgerRow.reservation_id,
            db.ModelAttemptRow.model_id,
            db.UsageLedgerRow.rate_card_id,
            db.UsageLedgerRow.charge_type,
            db.UsageLedgerRow.measured_or_estimated,
            func.sum(db.UsageLedgerRow.amount_micros),
        )
        .outerjoin(db.ModelAttemptRow, db.ModelAttemptRow.attempt_id == db.UsageLedgerRow.attempt_id)
        .where(db.UsageLedgerRow.reservation_id.in_(reservation_ids))
        .group_by(
            db.UsageLedgerRow.reservation_id,
            db.ModelAttemptRow.model_id,
            db.UsageLedgerRow.rate_card_id,
            db.UsageLedgerRow.charge_type,
            db.UsageLedgerRow.measured_or_estimated,
        )
    )
    api: dict[str, dict[str, int]] = {}
    gpu: dict[str, dict[str, int]] = {}
    labels: dict[str, dict[str, list[Provenance]]] = {}
    tools: dict[str, list[Charge]] = {}
    totals: dict[str, int] = {}
    for rid, model_id, rate_card_id, charge_type, label, amount in await session.execute(statement):
        reservation_id, amount = str(rid), int(amount)
        is_gpu = charge_type == ChargeType.GPU_ESTIMATE.value
        if not is_gpu:
            totals[reservation_id] = totals.get(reservation_id, 0) + amount
        if charge_type == ChargeType.TOOL_CALL.value:
            # A billed tool call counts in the shared scopes only; its label still decides the live domain.
            tool = Charge(str(rate_card_id or "tool"), amount, provenance=Provenance(label), kind="tool")
            tools.setdefault(reservation_id, []).append(tool)
            continue
        if model_id is None:
            continue  # an unattributed correction counts against shared scopes only
        target = gpu if is_gpu else api
        per = target.setdefault(reservation_id, {})
        per[model_id] = per.get(model_id, 0) + amount
        labels.setdefault(reservation_id, {}).setdefault(model_id, []).append(Provenance(label))
    result = {}
    for reservation_id in reservation_ids:
        models = set(api.get(reservation_id, {})) | set(gpu.get(reservation_id, {}))
        charges = [
            Charge(
                model_id=model_id,
                amount_micros=api.get(reservation_id, {}).get(model_id, 0),
                gpu_micros=gpu.get(reservation_id, {}).get(model_id, 0),
                provenance=least_trusted(labels[reservation_id][model_id]) or Provenance.MEASURED,
            )
            for model_id in sorted(models)
        ]
        charges.extend(tools.get(reservation_id, []))
        result[reservation_id] = LedgerSpend(totals.get(reservation_id, 0), charges)
    return result


def _allocations_json(commitments: dict[str, Commitment]) -> dict[str, Any]:
    return {
        key: {
            "reserved": c.reserved_micros,
            "committed": c.committed_micros,
            **({"provenance": c.provenance.value} if c.provenance else {}),
        }
        for key, c in commitments.items()
    }


class Reconciler:
    def __init__(
        self,
        quota: RedisQuotaService,
        sessionmaker: async_sessionmaker[AsyncSession],
        catalog: Catalog,
        *,
        stale_after: timedelta,
    ) -> None:
        self.quota = quota
        self.sessionmaker = sessionmaker
        self.catalog = catalog
        self.stale_after = stale_after

    async def run(self, tenant_id: str, *, triggered_by: str) -> ReconciliationResult:
        result = ReconciliationResult(
            reconciliation_id=new_id("rec"),
            tenant_id=tenant_id,
            triggered_by=triggered_by,
            started_at=self.quota.clock(),
        )
        if not await self.quota.acquire_lock(tenant_id, result.reconciliation_id, LOCK_TTL_SECONDS):
            raise ConflictError(
                "A budget reconciliation is already running for this tenant.",
                code="RECONCILIATION_IN_PROGRESS",
                remediation="Retry in a few minutes.",
            )
        drafts: list[alerts.AlertDraft] = []
        try:
            await self._stale_reservations(result, drafts)
            await self._redis_orphans(result)
            await self._ledger_differences(result, drafts)
            await self._counter_drift(result, drafts)
            result.status = "completed"
        except Exception as exc:
            result.status, result.error = "failed", f"{type(exc).__name__}: {exc}"[:500]
            logger.warning("Reconciliation %s failed", result.reconciliation_id, exc_info=True)
        finally:
            result.completed_at = self.quota.clock()
            try:
                async with self.sessionmaker() as session, session.begin():
                    session.add(result.row())
            finally:
                await self.quota.release_lock(tenant_id, result.reconciliation_id)
        await alerts.record_alerts(self.sessionmaker, drafts)
        return result

    # 1. Open reservations past the stale cut-off ------------------------------------------------------

    async def _stale_reservations(
        self, result: ReconciliationResult, drafts: list[alerts.AlertDraft]
    ) -> None:
        now = self.quota.clock()
        async with self.sessionmaker() as session, session.begin():
            rows = (
                await session.scalars(
                    select(db.BudgetReservationRow)
                    .where(
                        db.BudgetReservationRow.tenant_id == result.tenant_id,
                        db.BudgetReservationRow.status == "reserved",
                        _store_held(),
                        db.BudgetReservationRow.created_at < now - self.stale_after,
                    )
                    .with_for_update(skip_locked=True)
                )
            ).all()
            result.reservations_checked += len(rows)
            recorded = set(
                await session.scalars(
                    select(db.ExecutionRunRow.run_id).where(
                        db.ExecutionRunRow.run_id.in_([r.run_id for r in rows])
                    )
                )
            )
            spend = await ledger_spend(session, [r.reservation_id for r in rows if r.run_id in recorded])
            for row in rows:
                reservation = reservation_from_row(row)
                if row.run_id in recorded:
                    await self._settle_from_ledger(row, reservation, spend[row.reservation_id], now)
                    result.settled += 1
                    result.findings.append(
                        Finding(
                            "settled_from_ledger",
                            "The run finished but its settlement never reached the quota store; settled "
                            "from the usage ledger.",
                            reservation_id=row.reservation_id,
                            delta_micros=row.actual_micros,
                        )
                    )
                else:
                    await self.quota.release(reservation)
                    row.status, row.released_micros = "expired", row.estimate_micros
                    row.settled_at = row.reconciled_at = now
                    result.expired += 1
                    result.findings.append(
                        Finding(
                            "expired_orphan",
                            "No run record exists for this reservation; the hold was released.",
                            reservation_id=row.reservation_id,
                            delta_micros=-row.estimate_micros,
                        )
                    )
                    drafts.append(
                        alerts.expired_alert(
                            result.tenant_id,
                            amount=row.estimate_micros,
                            reservation_id=row.reservation_id,
                            run_id=row.run_id,
                            domain=domain_of(row.allocations),
                        )
                    )

    async def _settle_from_ledger(
        self,
        row: db.BudgetReservationRow,
        reservation: LedgerReservation,
        spend: LedgerSpend,
        now: datetime,
    ) -> None:
        commitments = commit(
            dict(reservation.allocations),
            spend.api_micros,
            spend.charges,
            self.quota.prices.pools,
            reservation.created_at,
        )
        released = max(0, row.estimate_micros - spend.api_micros)
        await self.quota.apply_settlement(reservation, commitments, released=released)
        label = least_trusted(c.provenance for c in spend.charges)
        row.status, row.actual_micros, row.released_micros = "settled", spend.api_micros, released
        row.allocations = _allocations_json(commitments)
        row.charge_provenance = label.value if label else None
        row.settled_at = row.reconciled_at = now

    # 2. Holds that exist only in Redis ----------------------------------------------------------------

    async def _redis_orphans(self, result: ReconciliationResult) -> None:
        keys = QuotaKeys(result.tenant_id)
        cutoff = int((self.quota.clock() - self.stale_after).timestamp() * 1000)
        records = await self.quota.redis.zrangebyscore(keys.open, "-inf", cutoff)
        if not records:
            return
        payloads = [await self.quota.redis.hget(str(record), "payload") for record in records]
        reservations = [reservation_from_payload(str(p)) for p in payloads if p]
        async with self.sessionmaker() as session:
            journaled = set(
                await session.scalars(
                    select(db.BudgetReservationRow.reservation_id).where(
                        db.BudgetReservationRow.reservation_id.in_([r.reservation_id for r in reservations])
                    )
                )
            )
        for reservation in reservations:
            if reservation.reservation_id in journaled:
                continue  # the journal row drives it (step 1)
            await self.quota.release(reservation)
            result.expired += 1
            result.findings.append(
                Finding(
                    "released_redis_orphan",
                    "A hold existed only in the quota store (its journal write failed); released.",
                    reservation_id=reservation.reservation_id,
                    delta_micros=-reservation.amount_micros,
                )
            )

    # 3. Settled reservations whose ledger changed ------------------------------------------------------

    async def _ledger_differences(
        self, result: ReconciliationResult, drafts: list[alerts.AlertDraft]
    ) -> None:
        now = self.quota.clock()
        R, L = db.BudgetReservationRow, db.UsageLedgerRow
        newer_rows = exists().where(L.reservation_id == R.reservation_id, L.created_at > R.reconciled_at)
        deltas: dict[str, int] = {}
        labels: list[Provenance] = []
        async with self.sessionmaker() as session, session.begin():
            rows = (
                await session.scalars(
                    select(R)
                    .where(
                        R.tenant_id == result.tenant_id,
                        R.status == "settled",
                        _store_held(),
                        or_(R.reconciled_at.is_(None), newer_rows),
                    )
                    .with_for_update(skip_locked=True)
                )
            ).all()
            result.reservations_checked += len(rows)
            ids = [r.reservation_id for r in rows]
            spend = await ledger_spend(session, ids)
            prior: dict[tuple[str, str], int] = {}
            if ids:
                adjustment_rows = await session.execute(
                    select(
                        db.BudgetAdjustmentRow.reservation_id,
                        db.BudgetAdjustmentRow.bucket,
                        func.sum(db.BudgetAdjustmentRow.delta_micros),
                    )
                    .where(db.BudgetAdjustmentRow.reservation_id.in_(ids))
                    .group_by(db.BudgetAdjustmentRow.reservation_id, db.BudgetAdjustmentRow.bucket)
                )
                prior = {(rid, bucket): int(total) for rid, bucket, total in adjustment_rows}
            for row in rows:
                reservation = reservation_from_row(row)
                ledger = spend[row.reservation_id]
                expected = commit(
                    dict(reservation.allocations),
                    ledger.api_micros,
                    ledger.charges,
                    self.quota.prices.pools,
                    reservation.created_at,
                )
                recorded = committed_from_row(row)
                changed = {}
                for key, commitment in expected.items():
                    delta = (
                        commitment.committed_micros
                        - recorded.get(key, 0)
                        - prior.get((row.reservation_id, key), 0)
                    )
                    if delta:
                        changed[key] = delta
                row.reconciled_at = now
                if not changed:
                    continue
                for key, delta in changed.items():
                    session.add(
                        db.BudgetAdjustmentRow(
                            tenant_id=result.tenant_id,
                            reservation_id=row.reservation_id,
                            bucket=key,
                            delta_micros=delta,
                            reason="ledger_correction",
                            reconciliation_id=result.reconciliation_id,
                        )
                    )
                    deltas[key] = deltas.get(key, 0) + delta
                    result.adjustments += 1
                # Every shared bucket moves by the same amount; report it from the tenant's bucket.
                shared = next(
                    (d for key, d in changed.items() if Bucket.parse(key).scope_type == "tenant"),
                    next(iter(changed.values())),
                )
                label = least_trusted(c.provenance for c in ledger.charges) or Provenance.MEASURED
                labels.append(label)
                result.findings.append(
                    Finding(
                        "ledger_adjustment",
                        f"The usage ledger differs from committed spend in {len(changed)} bucket(s); "
                        "corrections appended.",
                        reservation_id=row.reservation_id,
                        delta_micros=shared,
                    )
                )
                drafts.append(
                    alerts.adjustment_alert(
                        result.tenant_id,
                        amount=shared,
                        provenance=label,
                        reservation_id=row.reservation_id,
                        run_id=row.run_id,
                        domain=domain_of(changed),
                    )
                )
        if deltas:
            await self.quota.apply_adjustments(
                result.tenant_id, deltas, provenance=least_trusted(labels) or Provenance.MEASURED
            )

    # 4. Counter drift on quiescent budgeted buckets ------------------------------------------------------

    async def _counter_drift(self, result: ReconciliationResult, drafts: list[alerts.AlertDraft]) -> None:
        tenant = result.tenant_id
        now = self.quota.clock()
        book = await self.quota.book(tenant, refresh=True)
        buckets: dict[str, Bucket] = {}
        for budget in book.budgets:
            if budget.is_template or budget.period == "request":
                continue
            # Visitors' domains are short-lived and rebuilt from the journal when missing; not swept.
            for domain in (LIVE, ILLUSTRATIVE):
                for bucket in buckets_for(budget.scope_type, budget.scope_id, now, domain=domain):
                    if bucket.period == budget.period:
                        buckets[bucket.key] = bucket
        if not buckets:
            return
        states = await self.quota.bucket_states(tenant, list(buckets.values()))  # snapshot before the read
        keys = list(buckets)
        async with self.sessionmaker() as session:
            totals = await bucket_totals(session, tenant, keys)
            open_holds = {
                key: int(count)
                for key, count in await session.execute(_OPEN_HOLDS, {"tenant": tenant, "keys": keys})
            }
        for key, bucket in buckets.items():
            state = states.get(key)
            if state is None or state.seq is None or state.reserved or open_holds.get(key):
                continue
            expected = totals[key].spent
            if state.spent == expected:
                continue
            outcome = await self.quota.correct(tenant, bucket, seq=state.seq, spent=expected)
            if outcome != "corrected":
                continue
            result.counters_corrected += 1
            result.findings.append(
                Finding(
                    "counter_corrected",
                    "The live counter drifted from the ledger while idle; reset to the ledger value.",
                    bucket=key,
                    delta_micros=expected - state.spent,
                )
            )
            drafts.append(
                alerts.correction_alert(
                    book.for_bucket(bucket),
                    bucket,
                    tenant_id=tenant,
                    spent=expected,
                    delta=expected - state.spent,
                    provenance=state.provenance,
                )
            )


_OPEN_HOLDS = text(
    """
    SELECT a.key, count(*)
    FROM budget_reservation r CROSS JOIN LATERAL jsonb_each(r.allocations) AS a(key, value)
    WHERE r.tenant_id = :tenant AND r.status = 'reserved' AND r.allocations ?| :keys AND a.key = ANY(:keys)
    GROUP BY a.key
    """
).bindparams(bindparam("keys", type_=ARRAY(String)))


async def reconcile_forever(reconciler: Reconciler, tenant_ids: Sequence[str], interval_seconds: int) -> None:
    """Background loop in the gateway process (LLD §21.2: a controlled service for the POC)."""
    while True:
        await asyncio.sleep(interval_seconds)
        for tenant_id in tenant_ids:
            try:
                await reconciler.run(tenant_id, triggered_by="scheduler")
            except ConflictError:
                continue  # another process holds the lock
            except Exception:
                logger.warning("Scheduled reconciliation failed for %s", tenant_id, exc_info=True)
