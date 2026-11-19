"""Quality and assurance report (LLD §15, §21.7, ADR-0018): output assurance, CIM sufficiency, Context Guard
verdicts and answer-citation coverage.

Computed from the run records at request time, not from the aggregates, with the same scope, window and
filters as the other reports and no user dimension. Only codes and counts leave this module: Guard failure
messages, escalation reasons and check names are mapped to fixed identifiers, never echoed.

Provenance: a rate over model outputs (assurance, citation coverage) carries the least trustworthy label of
those runs; CIM sufficiency and Guard verdicts carry the label of the CIM packages they judged, and counts of
gateway decisions (bypass, skip) are measured. Every label is capped at `synthetic` for synthetic traffic,
like the aggregates (ADR-0015).
"""

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from aiva_gateway.contracts.reports import (
    AssuranceQuality,
    EvidenceCoverage,
    GuardFailureCount,
    GuardQuality,
    NamedCount,
    OutputDlpCounts,
    QualityBlocks,
    QualityGroup,
    QualityReport,
    QualityRow,
    QualityTrendPoint,
    ReportMetadata,
    SufficiencyQuality,
)
from aiva_gateway.core.enums import CimState, Provenance
from aiva_gateway.db import models as db
from aiva_gateway.finops.scopes import least_trusted
from aiva_gateway.reporting.dimensions import NONE, run_dimension_columns
from aiva_gateway.reporting.lines import run_label
from aiva_gateway.reporting.queries import ReportContext, designation_of, run_where
from aiva_gateway.reporting.totals import ratio

# Context Guard failure messages (optimize/context_guard.py) to stable kinds, first match wins. Rows that
# record `failure_codes` themselves are read as such.
_GUARD_KINDS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (kind, re.compile(pattern))
    for kind, pattern in (
        ("unknown_reference", r"^unknown evidence references:"),
        ("selected_and_omitted", r"^evidence both selected and omitted:"),
        ("empty_selection", r"^empty selection:"),
        ("required_field_missing", r"^required field missing:"),
        ("dangling_citation", r"^dangling citation:"),
        ("citation_lost", r"^citation lost:"),
        ("citation_header_lost", r"^citation header lost:"),
        ("wrapper_integrity", r"^evidence wrapper integrity:"),
        ("context_loss", r"^context loss:"),
        ("numeric_fidelity", r"^numeric fidelity:"),
        ("token_budget_exceeded", r"^token budget exceeded:"),
        ("reported_missing_but_present", r": reported missing but present in the evidence$"),
        ("source_not_selected", r": source .+ not in selected evidence$"),
        ("value_not_verbatim", r": value not found verbatim in source .+$"),
        ("value_not_preserved", r": value not preserved in the upstream payload$"),
        ("chronology", r": (unparseable timestamp|\d+ evidence timestamp\(s\) collapsed or dropped)$"),
    )
)
_GUARD_CODES = {kind for kind, _ in _GUARD_KINDS}
# Assurance escalation reasons (assurance/checks.py) to codes.
_ESCALATIONS = {"Empty model output.": "empty_output", "Output contained a secret.": "secret_in_output"}
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_DLP = {"clean": "clean", "redacted": "redacted", "blocked": "blocked"}
_GROUP_COLUMNS = {
    "application": "application_id",
    "context_profile": "context_profile",
    "execution_path": "execution_path",
}


def guard_failure_kind(message: str) -> str:
    """The stable kind of a stored Context Guard failure message; `other` when none matches."""
    return next((kind for kind, pattern in _GUARD_KINDS if pattern.search(message)), "other")


def _guard_kinds(details: dict[str, Any]) -> list[str]:
    codes = details.get("failure_codes")
    if isinstance(codes, list):
        return [code if code in _GUARD_CODES else "other" for code in codes]
    return [guard_failure_kind(str(message)) for message in details.get("failures", [])]


def _decision_label(designations: Iterable[str]) -> Provenance:
    return Provenance.SYNTHETIC if Provenance.SYNTHETIC.value in set(designations) else Provenance.MEASURED


@dataclass
class _Run:
    group: str
    bucket: datetime
    designation: str
    label: Provenance
    cim_state: str
    sufficiency: str | None
    assurance: Any | None
    package: Any | None


@dataclass
class _Blocks:
    """Accumulates one set of quality blocks (the totals, or one group)."""

    runs: int = 0
    designations: set[str] = field(default_factory=set)
    checked: int = 0
    passed: int = 0
    dlp: Counter[str] = field(default_factory=Counter)
    escalations: Counter[str] = field(default_factory=Counter)
    checks: Counter[str] = field(default_factory=Counter)
    output_labels: set[Provenance] = field(default_factory=set)
    cim_invoked: int = 0
    sufficiency: Counter[str] = field(default_factory=Counter)
    bypassed: int = 0
    skipped: int = 0
    package_labels: set[Provenance] = field(default_factory=set)
    packages: int = 0
    packages_passed: int = 0
    unverified_passages: int = 0
    recall_floor_runs: int = 0
    failures: Counter[str] = field(default_factory=Counter)
    coverage: list[float] = field(default_factory=list)
    coverage_labels: set[Provenance] = field(default_factory=set)

    def add(self, run: _Run) -> None:
        self.runs += 1
        self.designations.add(run.designation)
        if run.cim_state == CimState.BYPASSED_POLICY:
            self.bypassed += 1
        elif run.cim_state == CimState.SKIPPED_SUFFICIENT:
            self.skipped += 1
        elif run.cim_state == CimState.INVOKED:
            self.cim_invoked += 1
            self.sufficiency[run.sufficiency or "unknown"] += 1
        if run.package is not None:
            label = run_label(run.package.provenance, run.designation)
            self.package_labels.add(label)
            details = run.package.guard_details or {}
            if "failures" in details:
                self.packages += 1
                kinds = _guard_kinds(details)
                self.packages_passed += not kinds
                self.failures.update(kinds)
                unverified = details.get("relevance_unverified") or {}
                self.unverified_passages += int(unverified.get("omitted_passages", 0))
                self.recall_floor_runs += bool(details.get("recall_floor_added"))
        if run.assurance is not None:
            result = run.assurance
            self.checked += 1
            self.passed += bool(result.passed)
            self.dlp[_DLP.get(result.output_dlp, "not_run")] += 1
            if result.escalation:
                self.escalations[_ESCALATIONS.get(result.escalation, "other")] += 1
            self.checks.update(c if _IDENTIFIER.match(str(c)) else "other" for c in result.checks or [])
            self.output_labels.add(run.label)
            if result.evidence_coverage is not None:
                self.coverage.append(float(result.evidence_coverage))
                self.coverage_labels.add(run.label)

    def blocks(self) -> QualityBlocks:
        decisions = _decision_label(self.designations)
        coverage = self.coverage
        return QualityBlocks(
            request_count=self.runs,
            assurance=AssuranceQuality(
                checked_runs=self.checked,
                passed=ratio(self.passed, self.checked),
                output_dlp=OutputDlpCounts(**self.dlp),
                escalations=_named(self.escalations),
                checks=_named(self.checks),
                provenance=least_trusted(self.output_labels) or decisions,
            ),
            sufficiency=SufficiencyQuality(
                runs=self.runs,
                cim_invoked_runs=self.cim_invoked,
                verified=ratio(self.sufficiency["verified"], self.cim_invoked),
                cim_selected=ratio(self.sufficiency["cim-selected"], self.cim_invoked),
                fallback=ratio(self.sufficiency["failed-fallback-applied"], self.cim_invoked),
                bypassed_by_policy=ratio(self.bypassed, self.runs),
                skipped_sufficient=ratio(self.skipped, self.runs),
                provenance=least_trusted({*self.package_labels, decisions}) or decisions,
            ),
            guard=GuardQuality(
                packages=self.packages,
                passed=ratio(self.packages_passed, self.packages),
                relevance_unverified_passages=self.unverified_passages,
                recall_floor_added_runs=self.recall_floor_runs,
                failures=[
                    GuardFailureCount(kind=kind, count=count)
                    for kind, count in sorted(self.failures.items(), key=lambda item: (-item[1], item[0]))
                ],
                provenance=least_trusted(self.package_labels) or decisions,
            ),
            evidence_coverage=EvidenceCoverage(
                status="measured",
                measured_runs=len(coverage),
                mean=sum(coverage) / len(coverage),
                with_unresolved=ratio(sum(1 for value in coverage if value < 1.0), len(coverage)),
                provenance=least_trusted(self.coverage_labels),
            )
            if coverage
            else EvidenceCoverage(status="not_measured", measured_runs=0),
        )


def _named(counts: Counter[str]) -> list[NamedCount]:
    return [
        NamedCount(name=name, count=count)
        for name, count in sorted(counts.items(), key=lambda i: (-i[1], i[0]))
    ]


def _bucket(at: datetime, hourly: bool) -> datetime:
    at = at.astimezone(UTC)
    return (
        at.replace(minute=0, second=0, microsecond=0)
        if hourly
        else datetime(at.year, at.month, at.day, tzinfo=UTC)
    )


async def quality(
    session: AsyncSession, ctx: ReportContext, group_by: QualityGroup, threshold: float
) -> QualityReport:
    run = db.ExecutionRunRow
    assurance, package = aliased(db.AssuranceResultRow, name="ar"), aliased(db.ContextPackageRow, name="cp")
    statement, dims = run_dimension_columns(threshold)

    def latest(table: Any) -> Any:
        return select(func.max(table.id)).where(table.run_id == run.run_id).scalar_subquery()

    sufficiency = (
        select(db.OptimizationEventRow.preservation_result)
        .where(db.OptimizationEventRow.run_id == run.run_id)
        .order_by(db.OptimizationEventRow.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    rows = await session.execute(
        statement.outerjoin(assurance, assurance.id == latest(db.AssuranceResultRow))
        .outerjoin(package, package.id == latest(db.ContextPackageRow))
        .add_columns(
            assurance,
            package,
            sufficiency.label("sufficiency"),
            dims["taxonomy_version"].label("taxonomy_version"),
            dims["classifier_version"].label("classifier_version"),
        )
        .where(*run_where(ctx, dims))
    )
    hourly = ctx.hourly_trend
    totals, groups, trend = _Blocks(), dict[str, _Blocks](), dict[datetime, _Blocks]()
    versions: tuple[set[str], set[str]] = (set(), set())
    for row in rows:
        record: db.ExecutionRunRow = row[0]
        result, pack = row[1], row[2]
        if group_by == "context_profile":
            group = (pack.context_profile if pack is not None else None) or NONE
        else:
            group = str(getattr(record, _GROUP_COLUMNS[group_by]))
        item = _Run(
            group=group,
            bucket=_bucket(record.created_at, hourly),
            designation=record.data_designation,
            label=run_label(record.provenance, record.data_designation),
            cim_state=record.cim_state,
            sufficiency=row.sufficiency,
            assurance=result,
            package=pack,
        )
        totals.add(item)
        groups.setdefault(group, _Blocks()).add(item)
        trend.setdefault(item.bucket, _Blocks()).add(item)
        versions[0].add(str(row.taxonomy_version))
        versions[1].add(str(row.classifier_version))

    def label(group: str) -> str:
        if group_by == "context_profile":
            return "No CIM package" if group == NONE else group
        return ctx.label(_GROUP_COLUMNS[group_by], group)

    now = datetime.now(UTC)
    return QualityReport(
        metadata=ReportMetadata(
            window=ctx.window,
            filters=ctx.active_filters(),
            scope=ctx.scope.contract(),
            generated_at=now,
            aggregates_as_of=None,
            granularity="hour" if hourly else "day",
            taxonomy_versions=_versions(versions[0]),
            classifier_versions=_versions(versions[1]),
            pricing_snapshot=ctx.pricing_snapshot,
            data_designation=designation_of(totals.designations),
        ),
        computed_at=now,
        group_by=group_by,
        totals=totals.blocks(),
        rows=[
            QualityRow(group=group, label=label(group), **blocks.blocks().model_dump())
            for group, blocks in sorted(groups.items(), key=lambda item: (-item[1].runs, item[0]))
        ],
        trend=[_trend_point(bucket, trend.get(bucket)) for bucket in ctx.buckets()],
    )


def _trend_point(bucket: datetime, blocks: _Blocks | None) -> QualityTrendPoint:
    blocks = blocks or _Blocks()
    return QualityTrendPoint(
        bucket=bucket,
        request_count=blocks.runs,
        checked_runs=blocks.checked,
        passed_runs=blocks.passed,
        cim_invoked_runs=blocks.cim_invoked,
        verified_runs=blocks.sufficiency["verified"],
    )


def _versions(values: set[str]) -> list[str]:
    return sorted(v for v in values if v and v not in (NONE, "not-reached"))
