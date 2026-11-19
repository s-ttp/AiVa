"""Baseline assurance: output DLP (block secrets, redact identifiers when required), non-empty output, and
the answer-citation check when the answer's context is given (`citations.py`; it reports, never blocks)."""

from aiva_gateway.assurance.citations import CHECK as CITATIONS_CHECK
from aiva_gateway.assurance.citations import check_answer_citations
from aiva_gateway.pipeline.interfaces import AnswerContext, AssuranceOutcome, DlpScanner

_BLOCKING_TYPES = {"SECRET", "PRIVATE_KEY"}


class BaselineAssurance:
    def __init__(self, scanner: DlpScanner) -> None:
        self._scanner = scanner

    def check(
        self, content: str, *, output_dlp_required: bool, context: AnswerContext | None = None
    ) -> AssuranceOutcome:
        checks = ["non-empty-output", "output-dlp"]
        if not content.strip():
            return AssuranceOutcome(
                False, "not-run", content, tuple(checks), blocked_reason="Empty model output."
            )
        result = self._scanner.scan(content, detect_injection=False)
        types = tuple(result.finding_types)
        if _BLOCKING_TYPES & set(types):
            return AssuranceOutcome(
                False,
                "blocked",
                "",
                tuple(checks),
                blocked_reason="Output contained a secret.",
                findings=types,
            )
        redacted = bool(types) and output_dlp_required
        delivered = self._scanner.redact(content, result) if redacted else content
        citations = None
        if context is not None:
            checks.append(CITATIONS_CHECK)
            citations = check_answer_citations(delivered, context)
        return AssuranceOutcome(
            True,
            "redacted" if redacted else "clean",
            delivered,
            tuple(checks),
            findings=types,
            citations=citations,
        )
