"""FinOps alert store: idempotent writes for re-running writers (`record_alerts(on_conflict="ignore")`)."""

from sqlalchemy import select

from aiva_gateway.core.enums import Provenance
from aiva_gateway.core.ids import ulid
from aiva_gateway.db.models import BudgetAlertRow
from aiva_gateway.finops.alerts import AlertDraft, record_alerts
from aiva_gateway.finops.scopes import Bucket


def draft(alert_id: str | None, message: str) -> AlertDraft:
    return AlertDraft(
        tenant_id="gct",
        kind="threshold",
        message=message,
        provenance=Provenance.ESTIMATED,
        bucket=Bucket("application", "gct-support-chatbot", "daily", "2026-10-14"),
        alert_id=alert_id,
    )


async def stored(services, ids: list[str]) -> dict[str, str]:
    async with services.sessionmaker() as session:
        rows = await session.scalars(select(BudgetAlertRow).where(BudgetAlertRow.alert_id.in_(ids)))
        return {row.alert_id: row.message for row in rows}


async def test_ignore_skips_repeated_ids_and_keeps_the_rest(services) -> None:
    first, second, third = (f"alr_test{ulid()}" for _ in range(3))
    batch = [draft(first, "first"), draft(second, "second"), draft(first, "repeat in batch")]
    await record_alerts(services.sessionmaker, batch, on_conflict="ignore")
    assert await stored(services, [first, second]) == {first: "first", second: "second"}

    # A re-run over the same window: the existing alert is skipped, the new one is written.
    await record_alerts(
        services.sessionmaker, [draft(first, "re-run"), draft(third, "third")], on_conflict="ignore"
    )
    assert await stored(services, [first, second, third]) == {
        first: "first",
        second: "second",
        third: "third",
    }


async def test_default_write_is_unchanged(services) -> None:
    # Without a deterministic ID every draft gets a fresh one.
    marker = f"fresh {ulid()}"
    await record_alerts(services.sessionmaker, [draft(None, marker), draft(None, marker)])
    async with services.sessionmaker() as session:
        rows = (await session.scalars(select(BudgetAlertRow).where(BudgetAlertRow.message == marker))).all()
    assert len(rows) == 2 and rows[0].alert_id != rows[1].alert_id
    assert all(row.alert_id.startswith("alr_") and row.created_at is not None for row in rows)

    # A repeated ID still fails the whole batch (logged, never raised): existing callers see no change.
    repeated, other = f"alr_test{ulid()}", f"alr_test{ulid()}"
    await record_alerts(
        services.sessionmaker, [draft(repeated, "a"), draft(other, "b"), draft(repeated, "c")]
    )
    assert await stored(services, [repeated, other]) == {}
