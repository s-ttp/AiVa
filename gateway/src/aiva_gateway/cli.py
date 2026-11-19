"""`aiva-admin`: migrations, seeding, credentials, dev tokens, knowledge-base ingest, Insights, contracts."""

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from aiva_gateway.config import REPO_ROOT, get_settings


def _migrate(_: argparse.Namespace) -> None:
    alembic_ini = str(REPO_ROOT / "gateway" / "alembic.ini")
    subprocess.run([sys.executable, "-m", "alembic", "-c", alembic_ini, "upgrade", "head"], check=True)  # noqa: S603


async def _seed(args: argparse.Namespace) -> None:
    from aiva_gateway.catalog.seed import create_application_credential, seed_reference_data
    from aiva_gateway.finops.quota import invalidate_budgets
    from aiva_gateway.services import build_services

    services = build_services(get_settings())
    try:
        async with services.sessionmaker() as session, session.begin():
            await seed_reference_data(services, session)
            credentials = {}
            if args.with_dev_credentials:
                for app in services.catalog.tenant.applications:
                    credentials[app.id] = await create_application_credential(
                        services, session, app.id, "dev"
                    )
        # Budgets may have changed: make every running gateway reload them before its next reservation.
        await invalidate_budgets(services.redis, services.catalog.tenant.tenant.id)
        print("Seeded reference data and published the policy bundle.")
        for app_id, credential in credentials.items():
            print(f"{app_id}: {credential}")
    finally:
        await services.aclose()


async def _create_credential(args: argparse.Namespace) -> None:
    from aiva_gateway.catalog.seed import create_application_credential
    from aiva_gateway.services import build_services

    services = build_services(get_settings())
    try:
        async with services.sessionmaker() as session, session.begin():
            print(await create_application_credential(services, session, args.application, args.label))
    finally:
        await services.aclose()


async def _kb_ingest(args: argparse.Namespace) -> None:
    from aiva_gateway.optimize.embeddings import build_embedder
    from aiva_gateway.optimize.knowledge import ingest_documents, load_jsonl
    from aiva_gateway.services import build_services

    settings = get_settings()
    services = build_services(settings)
    try:
        documents = load_jsonl(Path(args.file))
        async with services.sessionmaker() as session, session.begin():
            report = await ingest_documents(
                session,
                tenant_id=args.tenant,
                documents=documents,
                embedder=build_embedder(settings.embedding_backend),
                dlp=services.dlp,
            )
        print(
            f"Ingested {report.documents} documents, {report.chunks} chunks "
            f"({report.dlp_flagged_chunks} held back by DLP, {report.injection_flagged_chunks} by "
            f"prompt-injection screening) with {settings.embedding_backend} embeddings."
        )
    finally:
        await services.aclose()


async def _reconcile(args: argparse.Namespace) -> None:
    from aiva_gateway.finops.quota import RedisQuotaService
    from aiva_gateway.finops.reconcile import Reconciler
    from aiva_gateway.services import build_services

    services = build_services(get_settings())
    try:
        assert isinstance(services.quota, RedisQuotaService)
        reconciler = Reconciler(
            services.quota,
            services.sessionmaker,
            services.catalog,
            stale_after=services.settings.reservation_stale,
        )
        result = await reconciler.run(args.tenant, triggered_by="cli")
        print(
            f"{result.reconciliation_id} {result.status}: checked {result.reservations_checked}, "
            f"settled {result.settled}, expired {result.expired}, adjustments {result.adjustments}, "
            f"counters corrected {result.counters_corrected}"
        )
        for finding in result.findings:
            print(f"  {finding.kind} {finding.reservation_id or finding.bucket}: {finding.detail}")
        if result.error:
            print(f"error: {result.error}")
            sys.exit(1)
    finally:
        await services.aclose()


async def _ledger_adjust(args: argparse.Namespace) -> None:
    from aiva_gateway.finops.ledger import post_ledger_adjustment
    from aiva_gateway.services import build_services

    services = build_services(get_settings())
    try:
        async with services.sessionmaker() as session, session.begin():
            row = await post_ledger_adjustment(
                session,
                services.catalog,
                run_id=args.run,
                attempt_id=args.attempt,
                amount_micros=args.amount_micros,
                reason=args.reason,
                actor_id=args.actor,
            )
        print(f"{row.ledger_id}: {row.amount_micros} micro-USD on {row.run_id}; run `aiva-admin reconcile`.")
    finally:
        await services.aclose()


def _parse_day(value: str | None) -> datetime | None:
    from datetime import UTC, date, time

    if value is None:
        return None
    return datetime.combine(date.fromisoformat(value), time.min, tzinfo=UTC)


async def _aggregate(args: argparse.Namespace) -> None:
    from datetime import timedelta

    from aiva_gateway.reporting.worker import build_aggregator
    from aiva_gateway.services import build_services

    services = build_services(get_settings())
    try:
        start = _parse_day(args.start)
        end = _parse_day(args.end)
        if (start is None) != (end is None):
            sys.exit("Give both --from and --to, or neither for an incremental refresh.")
        result = await build_aggregator(services).refresh(
            args.tenant, triggered_by="cli", start=start, end=end + timedelta(days=1) if end else None
        )
        print(
            f"{result.refresh_id} {result.status}: {len(result.hours)} hours, {len(result.days)} days, "
            f"{result.runs_counted} runs, {result.lines_written} lines, {result.alerts_raised} new alerts"
        )
        if result.error:
            print(f"error: {result.error}")
            sys.exit(1)
    finally:
        await services.aclose()


async def _traffic(args: argparse.Namespace) -> None:
    from datetime import UTC

    from aiva_gateway.reporting.aggregate import HOUR, floor_hour
    from aiva_gateway.reporting.traffic import ALLOWED_MODES, generate_traffic, plan_traffic
    from aiva_gateway.reporting.worker import build_aggregator
    from aiva_gateway.services import build_services

    settings = get_settings()
    if settings.inference_mode not in ALLOWED_MODES:
        sys.exit(
            "aiva-admin traffic runs only with AIVA_INFERENCE_MODE simulated or replay (no provider spend)."
        )
    # Synthetic data; no embedding model is loaded (explicit evidence, no retrieval, exact cache only).
    settings = settings.model_copy(
        update={"data_designation": "synthetic", "semantic_cache_enabled": False, "retrieval_enabled": False}
    )
    services = build_services(settings)
    try:
        started = datetime.now(UTC)
        report = await generate_traffic(
            services, plan=plan_traffic(args.runs, seed=args.seed), tenant_id=args.tenant
        )
        print(f"Generated {len(report.runs)} synthetic runs ({settings.inference_mode} inference).")
        for title, counter in (
            ("applications", report.applications),
            ("paths", report.paths),
            ("outcomes", report.outcomes),
        ):
            print(f"  {title}: " + ", ".join(f"{k}={v}" for k, v in sorted(counter.items())))
        result = await build_aggregator(services).refresh(
            args.tenant,
            triggered_by="traffic",
            start=floor_hour(started),
            end=floor_hour(datetime.now(UTC)) + HOUR,
        )
        print(f"Aggregates {result.status}: {result.runs_counted} runs, {result.alerts_raised} new alerts.")
    finally:
        await services.aclose()


def _mint_token(args: argparse.Namespace) -> None:
    from aiva_gateway.auth.tokens import mint_user_token

    print(mint_user_token(get_settings(), user_id=args.user, tenant_id=args.tenant, ttl_seconds=args.ttl))


def _export_contracts(args: argparse.Namespace) -> None:
    from aiva_gateway.contracts.cim import CimRequest, CimResponse
    from aiva_gateway.contracts.events import EventEnvelope
    from aiva_gateway.contracts.policy import PolicyDecision, PolicyInput
    from aiva_gateway.main import create_app

    out = Path(args.out)
    (out / "schemas").mkdir(parents=True, exist_ok=True)
    documents = {
        out / "openapi.json": create_app().openapi(),
        out / "schemas" / "event-envelope.schema.json": EventEnvelope.model_json_schema(),
        out / "schemas" / "cim-request.schema.json": CimRequest.model_json_schema(),
        out / "schemas" / "cim-response.schema.json": CimResponse.model_json_schema(),
        out / "schemas" / "policy-input.schema.json": PolicyInput.model_json_schema(),
        out / "schemas" / "policy-decision.schema.json": PolicyDecision.model_json_schema(),
    }
    for path, document in documents.items():
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {path}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="aiva-admin")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="Apply database migrations").set_defaults(func=_migrate)
    seed = sub.add_parser("seed", help="Load config/ into the database and publish policies to OPA")
    seed.add_argument(
        "--with-dev-credentials", action="store_true", help="Print a credential per application"
    )
    seed.set_defaults(coro=_seed)
    cred = sub.add_parser("create-credential", help="Create an application credential (printed once)")
    cred.add_argument("--application", required=True)
    cred.add_argument("--label", default="manual")
    cred.set_defaults(coro=_create_credential)
    reconcile = sub.add_parser("reconcile", help="Reconcile budget reservations with the usage ledger")
    reconcile.add_argument("--tenant", default="gct")
    reconcile.set_defaults(coro=_reconcile)
    adjust = sub.add_parser("ledger-adjust", help="Append a usage-ledger correction (never an update)")
    adjust.add_argument("--run", required=True)
    adjust.add_argument("--attempt", default=None, help="Attempt the correction applies to (model scope)")
    adjust.add_argument("--amount-micros", type=int, required=True, help="Signed amount in micro-USD")
    adjust.add_argument("--reason", required=True)
    adjust.add_argument("--actor", default="aiva-admin")
    adjust.set_defaults(coro=_ledger_adjust)
    token = sub.add_parser("mint-token", help="Mint a user JWT for a seeded demo user")
    token.add_argument("--user", required=True)
    token.add_argument("--tenant", default="gct")
    token.add_argument("--ttl", type=int, default=3600)
    token.set_defaults(func=_mint_token)
    kb = sub.add_parser(
        "kb-ingest", help="Load a JSON Lines knowledge base for retrieval (replaces documents)"
    )
    kb.add_argument("file")
    kb.add_argument("--tenant", default="gct")
    kb.set_defaults(coro=_kb_ingest)
    aggregate = sub.add_parser(
        "aggregate", help="Refresh the Insights aggregates (incremental, or whole days with --from/--to)"
    )
    aggregate.add_argument("--tenant", default="gct")
    aggregate.add_argument("--from", dest="start", default=None, help="First UTC day (YYYY-MM-DD)")
    aggregate.add_argument("--to", dest="end", default=None, help="Last UTC day, inclusive")
    aggregate.set_defaults(coro=_aggregate)
    traffic = sub.add_parser(
        "traffic",
        help="Run synthetic traffic through the pipeline (simulated or replay inference only)",
        description=(
            "Run synthetic traffic through the pipeline (simulated or replay inference only). OPA must "
            "hold the published policy bundle: after restarting OPA, start the gateway once (it re-pushes "
            "the bundle; `make seed` does not when the policy is unchanged), or every run fails closed "
            "with POLICY_ENGINE_UNAVAILABLE."
        ),
    )
    traffic.add_argument("--runs", type=int, default=60, help="Number of requests (at least the core plan)")
    traffic.add_argument("--seed", type=int, default=7)
    traffic.add_argument("--tenant", default="gct")
    traffic.set_defaults(coro=_traffic)
    export = sub.add_parser("export-contracts", help="Write contracts/openapi.json and JSON schemas")
    export.add_argument("--out", default=str(REPO_ROOT / "contracts"))
    export.set_defaults(func=_export_contracts)
    args = parser.parse_args(argv)
    if hasattr(args, "coro"):
        asyncio.run(args.coro(args))
    else:
        args.func(args)


if __name__ == "__main__":
    main()
