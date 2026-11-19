"""Draft a document through one governed run, build it, open it in the sandbox and check it (LLD §15 #10).

The drafting run is an ordinary chat run: policy, reservation, egress checks, assurance (output DLP) and
metering all apply. Confidential sources take the sovereign direct path, so the CIM is bypassed and no
external provider sees them. The model writes prose. The gateway builds the editable file, then opens
that file in the network-less sandbox, and the checks run on what the sandbox read.
"""

import hashlib
import re
from dataclasses import dataclass

from opentelemetry.sdk.trace import ReadableSpan

from aiva_gateway.artifacts import sandbox
from aiva_gateway.artifacts.builder import DocumentFrame, build_document
from aiva_gateway.artifacts.checks import CheckReport, check_document
from aiva_gateway.artifacts.draft import DRAFT_SYSTEM_PROMPT, parse_draft
from aiva_gateway.artifacts.sandbox import Rendered, SandboxError
from aiva_gateway.auth.principal import RunIdentity
from aiva_gateway.contracts.artifacts import DOCUMENT_MEDIA_TYPES, ArtifactCheck, ArtifactCreateRequest
from aiva_gateway.contracts.chat import ChatRequest
from aiva_gateway.contracts.messages import ChatMessage
from aiva_gateway.core.enums import Provenance, RunStatus
from aiva_gateway.core.ids import new_id
from aiva_gateway.pipeline.context import RunContext
from aiva_gateway.pipeline.orchestrator import Orchestrator
from aiva_gateway.services import Services

SCENARIO_ID = "document-copilot"
TASK = "generation"
_SLUG = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class GeneratedArtifact:
    artifact_id: str
    document_type: str
    title: str
    file_name: str
    media_type: str
    content: bytes
    sha256: str
    provenance: Provenance
    report: CheckReport
    rendered: Rendered | None

    @property
    def status(self) -> str:
        return "ready" if self.report.passed else "failed_checks"


@dataclass
class Draft:
    ctx: RunContext
    spans: list[ReadableSpan]
    artifact: GeneratedArtifact | None


def file_name(title: str, document_type: str) -> str:
    slug = _SLUG.sub("-", title.lower()).strip("-")[:80] or "document"
    return f"{slug}.{document_type}"


def drafting_prompt(body: ArtifactCreateRequest) -> str:
    lines = [f"Title: {body.title}", "", body.instructions]
    if body.figures:
        lines += ["", "Critical figures to keep exactly as written, with their source:"]
        lines += [
            f"- {f.label}: {f.value}{' ' + f.unit if f.unit else ''} [{f.source_id}]" for f in body.figures
        ]
    return "\n".join(lines)


class DocumentCopilot:
    def __init__(self, services: Services) -> None:
        self.s = services

    def chat_request(self, body: ArtifactCreateRequest) -> ChatRequest:
        return ChatRequest(
            messages=[
                ChatMessage(role="system", content=DRAFT_SYSTEM_PROMPT),
                ChatMessage(role="user", content=drafting_prompt(body)),
            ],
            evidence=body.evidence,
            data_classification=body.data_classification,
            task=TASK,
            scenario_id=body.scenario_id or SCENARIO_ID,
            max_output_tokens=body.max_output_tokens,
            trace=body.trace,
        )

    async def draft(self, body: ArtifactCreateRequest, identity: RunIdentity, tenant_id: str) -> Draft:
        orchestrator = Orchestrator(self.s)
        ctx = orchestrator.new_context(self.chat_request(body), identity, tenant_id)
        spans = await orchestrator.run(ctx)
        if ctx.status is not RunStatus.COMPLETED or not ctx.response_content:
            return Draft(ctx, spans, None)
        return Draft(ctx, spans, await self._build(body, ctx))

    async def _build(self, body: ArtifactCreateRequest, ctx: RunContext) -> GeneratedArtifact:
        model = ctx.selected_model.display_name if ctx.selected_model else "unknown model"
        classification = ctx.classification.effective.value if ctx.classification else "internal"
        provenance = ctx.response_provenance
        frame = DocumentFrame(
            provenance_note=(
                f"Synthetic GCT data. Classification: {classification}. Draft text: {provenance.value} "
                f"({model}). Key figures and sources are copied from the inputs by AiVa."
            ),
            figures=list(body.figures),
            source_ids=[e.id for e in body.evidence],
        )
        assert ctx.response_content is not None
        content = build_document(body.document_type, parse_draft(body.title, ctx.response_content), frame)
        digest = hashlib.sha256(content).hexdigest()
        output_dlp = ctx.assurance.output_dlp if ctx.assurance else "not-run"
        rendered: Rendered | None = None
        try:
            rendered = await sandbox.render(content, body.document_type, expected_sha256=digest)
            report = check_document(
                rendered,
                title=body.title,
                instructions=body.instructions,
                evidence=list(body.evidence),
                figures=list(body.figures),
                output_dlp=output_dlp,
            )
        except SandboxError as exc:
            report = _unrendered(exc)
        return GeneratedArtifact(
            artifact_id=new_id("art"),
            document_type=body.document_type,
            title=body.title,
            file_name=file_name(body.title, body.document_type),
            media_type=DOCUMENT_MEDIA_TYPES[body.document_type],
            content=content,
            sha256=digest,
            provenance=provenance,
            report=report,
            rendered=rendered,
        )


def _unrendered(exc: SandboxError) -> CheckReport:
    from aiva_gateway.contracts.artifacts import DocumentOutline

    checks = [
        ArtifactCheck(name=name, status="not-run", detail="Not checked: the sandbox could not open the file")
        for name in ("structure", "figures", "numbers", "citations", "editability", "output_dlp")
    ]
    checks.append(
        ArtifactCheck(name="sandbox_render", status="failed", detail=exc.message, failures=[exc.code])
    )
    return CheckReport(
        checks=checks,
        figures=[],
        citations=[],
        outline=DocumentOutline(sections=0, paragraphs=0, tables=0, editable=False),
    )
