"""The Insights background worker in the gateway process (LLD §21.2: a controlled service for the POC).

Every `AIVA_AGGREGATE_INTERVAL_SECONDS` it refreshes the aggregates touched since the last refresh, evaluates
the anomaly rules for those hours, records exports orphaned by a restart as failed and deletes the content of
expired CSV exports.
"""

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy.exc import SQLAlchemyError

from aiva_gateway.reporting.aggregate import Aggregator
from aiva_gateway.reporting.anomalies import AnomalyDetector, load_insights_config
from aiva_gateway.reporting.exports import STALE_AFTER, fail_interrupted, purge_expired
from aiva_gateway.services import Services

logger = logging.getLogger(__name__)


def build_aggregator(services: Services, *, anomalies: bool = True) -> Aggregator:
    detector = None
    if anomalies:
        rules = load_insights_config(services.settings.config_dir).anomaly_rules
        detector = AnomalyDetector(services.sessionmaker, rules)
    return Aggregator(services.sessionmaker, services.catalog, services.prices, anomalies=detector)


async def sweep_exports(services: Services, tenant_ids: Sequence[str], *, created_before: datetime) -> None:
    """Fail exports orphaned before `created_before` and purge expired content; never raises."""
    try:
        async with services.sessionmaker() as session, session.begin():
            interrupted = await fail_interrupted(session, tenant_ids, created_before=created_before)
            await purge_expired(session, tenant_ids)
        if interrupted:
            logger.warning("Recorded %d interrupted CSV exports as failed", len(interrupted))
    except SQLAlchemyError:
        logger.warning("Could not sweep CSV exports", exc_info=True)


async def insights_forever(
    services: Services, tenant_ids: Sequence[str], interval_seconds: int, *, started_at: datetime
) -> None:
    aggregator = build_aggregator(services)
    # No export of this process existed before it started: anything older still in flight was orphaned.
    await sweep_exports(services, tenant_ids, created_before=started_at)
    while True:
        for tenant_id in tenant_ids:
            try:
                await aggregator.refresh(tenant_id, triggered_by="scheduler")
            except Exception:
                logger.warning("Scheduled Insights refresh failed for %s", tenant_id, exc_info=True)
        await sweep_exports(services, tenant_ids, created_before=datetime.now(UTC) - STALE_AFTER)
        await asyncio.sleep(interval_seconds)


def start_insights(services: Services) -> "asyncio.Task[None] | None":
    interval = services.settings.aggregate_interval_seconds
    if interval <= 0:
        return None
    tenants = [services.catalog.tenant.tenant.id]
    started_at = datetime.now(UTC)  # before this process serves any export request
    return asyncio.create_task(
        insights_forever(services, tenants, interval, started_at=started_at), name="aiva-insights"
    )
