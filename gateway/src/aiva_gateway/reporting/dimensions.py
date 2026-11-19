"""Report dimensions of a run, computed in SQL so aggregates and trace lists share one definition.

Classification labels follow LLD §21.3: a reviewed label wins; a low-confidence or review-needed prediction
is reported as `unclassified` rather than asserted as fact. Only the RLCD-predicted dimensions (task
category, complexity) can be unclassified; business function comes from the authenticated application's
department, sensitivity from AI-PEL and language from deterministic detection.
"""

from typing import Any

from sqlalchemy import (
    Float,
    Select,
    SQLColumnExpression,
    String,
    and_,
    case,
    cast,
    func,
    literal,
    or_,
    select,
)
from sqlalchemy.orm import aliased

from aiva_gateway.auth.principal import SERVICE_PREFIX as SERVICE_PREFIX  # re-exported for reports
from aiva_gateway.db.models import (
    ClassificationEventRow,
    ExecutionRunRow,
    OptimizationEventRow,
    PolicyEventRow,
)

UNCLASSIFIED = "unclassified"
NONE = "none"
UNKNOWN = "unknown"
# A user run recorded without a pseudonym: reported as unattributed, never by another identifier.
UNATTRIBUTED = "unattributed"


def principal_of(kind: str, pseudonym: str | None, application_id: str) -> str:
    """How reports name a run's principal: its pseudonym, the application's service principal, or
    `unattributed`. Never a user ID (ADR-0015); the SQL form is the `principal` dimension below."""
    if pseudonym:
        return pseudonym
    return f"{SERVICE_PREFIX}{application_id}" if kind == "application" else UNATTRIBUTED


# Every dimension column of the aggregates, in line-key order (db.models.ReportLineColumns).
RUN_DIMENSIONS = (
    "application_id",
    "department_id",
    "cost_center_id",
    "principal_kind",
    "principal",
    "business_function",
    "task_category",
    "intent",
    "complexity",
    "sensitivity",
    "language",
    "classification_status",
    "taxonomy_version",
    "classifier_version",
    "execution_path",
    "context_strategy",
    "cim_state",
    "outcome",
    "source_policy",
    "policy_version",
    "deny_rule_id",
    "environment",
    "data_designation",
)
LINE_DIMENSIONS = ("line_kind", "model_id", "provider", "stage", "provenance", "rate_card_version")
DIMENSIONS = RUN_DIMENSIONS + LINE_DIMENSIONS


def _reviewed(event: Any, key: str) -> SQLColumnExpression[Any]:
    column: SQLColumnExpression[Any] = event.reviewed_label[key].astext
    return column


def run_dimension_columns(threshold: float) -> tuple[Select[Any], dict[str, SQLColumnExpression[Any]]]:
    """A select of execution_run joined to its latest classification and optimization events.

    Returns the select (no columns yet beyond the run) and the labelled dimension expressions, so callers
    can both project and filter on them.
    """
    run = ExecutionRunRow
    event = aliased(ClassificationEventRow, name="ce")
    optimization = aliased(OptimizationEventRow, name="oe")
    latest_event = (
        select(func.max(ClassificationEventRow.id))
        .where(ClassificationEventRow.run_id == run.run_id)
        .scalar_subquery()
    )
    latest_optimization = (
        select(func.max(OptimizationEventRow.id))
        .where(OptimizationEventRow.run_id == run.run_id)
        .scalar_subquery()
    )
    first_reason = (
        select(
            func.coalesce(
                PolicyEventRow.reasons[0]["rule_id"].astext,
                PolicyEventRow.matched_rule_ids[0].astext,
            )
        )
        .where(PolicyEventRow.run_id == run.run_id, PolicyEventRow.action != "allow")
        .order_by(PolicyEventRow.sequence)
        .limit(1)
        .scalar_subquery()
    )
    confidence = cast(event.confidences["rlcd"].astext, Float)
    low_confidence = or_(
        event.id.is_(None),
        event.review_needed.is_(True),
        and_(event.confidences.has_key("rlcd"), confidence < threshold),
    )

    def predicted(name: str, column: SQLColumnExpression[Any]) -> SQLColumnExpression[Any]:
        return case(
            (_reviewed(event, name).is_not(None), _reviewed(event, name)),
            (or_(low_confidence, column.is_(None)), literal(UNCLASSIFIED)),
            else_=column,
        )

    task_category = predicted("task_category", event.task_category)
    columns: dict[str, SQLColumnExpression[Any]] = {
        "application_id": run.application_id,
        "department_id": func.coalesce(run.department_id, NONE),
        "cost_center_id": func.coalesce(run.cost_center_id, NONE),
        "principal_kind": run.principal_kind,
        "principal": case(
            (run.principal_pseudonym.is_not(None), run.principal_pseudonym),
            (run.principal_kind == "application", literal(SERVICE_PREFIX, String) + run.application_id),
            else_=literal(UNATTRIBUTED, String),
        ),
        "business_function": func.coalesce(
            _reviewed(event, "business_function"), event.business_function, UNKNOWN
        ),
        "task_category": task_category,
        "intent": func.coalesce(_reviewed(event, "intent"), event.intent, NONE),
        "complexity": predicted("complexity", event.complexity),
        "sensitivity": run.effective_classification,
        "language": func.coalesce(event.language, UNKNOWN),
        "classification_status": case(
            (event.reviewed_label.is_not(None), literal("reviewed")),
            (task_category == UNCLASSIFIED, literal(UNCLASSIFIED)),
            else_=literal("classified"),
        ),
        "taxonomy_version": func.coalesce(event.taxonomy_version, NONE),
        "classifier_version": func.coalesce(event.classifier_version, NONE),
        "execution_path": run.execution_path,
        "context_strategy": func.coalesce(optimization.context_strategy, NONE),
        "cim_state": run.cim_state,
        "outcome": run.outcome,
        "source_policy": func.coalesce(run.ingress_source_policy, NONE),
        "policy_version": func.coalesce(run.policy_version, NONE),
        "deny_rule_id": func.coalesce(first_reason, ""),
        "environment": run.environment,
        "data_designation": run.data_designation,
    }
    statement = (
        select(run)
        .select_from(run)
        .outerjoin(event, event.id == latest_event)
        .outerjoin(optimization, optimization.id == latest_optimization)
    )
    return statement, columns
