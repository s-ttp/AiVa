"""Confidential DOCX/PPTX copilot contracts (LLD §14 Artifacts, §15 #10, §16 Document outputs, §17).

An artifact is drafted by one governed chat run (policy, route, reservation and metering as for any run;
confidential sources take the sovereign direct path), then built as an editable Office document by the
gateway. Content and preview are owner-only: other users, other applications and other tenants get 404,
and the attempt is audited (LLD §17 "Cross-tenant cache or artifact access -> deny and audit").
"""

from datetime import datetime
from typing import Literal

from pydantic import Field

from aiva_gateway.contracts.chat import ChatResponse
from aiva_gateway.contracts.common import ContractModel, PageInfo

# CriticalFigure and DocumentType are shared with the scenario catalog, so they live in contracts.messages.
from aiva_gateway.contracts.messages import CriticalFigure as CriticalFigure
from aiva_gateway.contracts.messages import DocumentType as DocumentType
from aiva_gateway.contracts.messages import EvidenceList, TestProfile
from aiva_gateway.core.enums import Classification, ExecutionPath, Provenance

ArtifactStatus = Literal["ready", "failed_checks"]
CheckStatus = Literal["passed", "failed", "not-run"]
CheckName = Literal[
    "structure", "figures", "numbers", "citations", "editability", "output_dlp", "sandbox_render"
]

DOCUMENT_MEDIA_TYPES: dict[str, str] = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


class ArtifactCreateRequest(ContractModel):
    document_type: DocumentType
    title: str = Field(min_length=1, max_length=200)
    instructions: str = Field(
        min_length=1, max_length=20_000, description="What to draft, e.g. 'Draft a board summary…'."
    )
    evidence: EvidenceList = Field(
        min_length=1,
        max_length=50,
        description="Source documents, cited by ID. Untrusted data, never instructions (LLD §17).",
    )
    figures: list[CriticalFigure] = Field(
        default_factory=list,
        max_length=100,
        description="Critical figures checked for exact preservation in the generated document.",
    )
    data_classification: Classification | None = Field(
        default=None,
        description="Declared classification. The most restrictive of declared and detected wins.",
    )
    scenario_id: str | None = None
    max_output_tokens: int = Field(default=2048, ge=256, le=8192)
    trace: bool = Field(default=False, description="Include the drafting run's span list in the response.")
    test_profile: TestProfile | None = None


class ArtifactCheck(ContractModel):
    """One document-output check (LLD §16 "Document outputs")."""

    name: CheckName
    status: CheckStatus
    detail: str = Field(description="UI copy describing what was checked.")
    failures: list[str] = Field(
        default_factory=list, description="Figure, citation or element IDs only; never document text."
    )


class FigureResult(ContractModel):
    figure_id: str
    preserved: bool = Field(description="The exact source value appears in the document.")
    occurrences: int
    locations: list[str] = Field(
        default_factory=list, description="Where it appears, e.g. 'slide 2', 'table 1', 'paragraph 4'."
    )


class CitationResult(ContractModel):
    source_id: str
    cited: bool


class DocumentOutline(ContractModel):
    sections: int = Field(description="Headings in a DOCX; slides in a PPTX.")
    paragraphs: int
    tables: int
    editable: bool = Field(description="Native, unprotected Office text, tables and placeholders.")


class ArtifactOut(ContractModel):
    artifact_id: str
    run_id: str = Field(description="The governed run that drafted the content.")
    trace_id: str
    tenant_id: str
    application_id: str
    document_type: DocumentType
    file_name: str
    media_type: str
    size_bytes: int
    sha256: str = Field(description="Hash of the stored file; only files the gateway generated are rendered.")
    status: ArtifactStatus
    classification: Classification = Field(description="Effective classification of the drafting run.")
    execution_path: ExecutionPath
    selected_model: str | None
    provenance: Provenance = Field(description="Provenance of the drafted text, e.g. simulated, measured.")
    generator_version: str
    checks: list[ArtifactCheck]
    figures: list[FigureResult] = Field(default_factory=list)
    citations: list[CitationResult] = Field(default_factory=list)
    outline: DocumentOutline
    access_policy: Literal["owner-only"] = "owner-only"
    data_designation: Literal["synthetic", "actual"]
    created_at: datetime


class ArtifactCreateResponse(ContractModel):
    run: ChatResponse = Field(
        description="The drafting run: policy decision, route, CIM state, tokens, cost and provenance."
    )
    artifact: ArtifactOut | None = Field(
        default=None, description="Absent when the run was blocked, needs approval or failed."
    )


class ArtifactList(ContractModel):
    items: list[ArtifactOut]
    page: PageInfo


class PreviewBlock(ContractModel):
    kind: Literal["heading", "paragraph", "bullet", "table"]
    level: int | None = Field(default=None, description="Heading or bullet level.")
    text: str | None = None
    rows: list[list[str]] | None = Field(default=None, description="Table cells, row by row.")


class PreviewPage(ContractModel):
    index: int
    kind: Literal["section", "slide"]
    title: str | None = None
    blocks: list[PreviewBlock]


class SandboxReport(ContractModel):
    """How the preview was produced (PLAN §3: sandboxed, network-less subprocess; generated files only)."""

    renderer: str
    network: Literal["disabled"] = "disabled"
    isolated_interpreter: bool
    timeout_ms: int
    duration_ms: float
    verified_sha256: str = Field(
        description="The file hash checked against the stored artifact before rendering."
    )


class ArtifactPreview(ContractModel):
    """Structured preview as plain text blocks (no HTML), rendered from the stored file in the sandbox."""

    artifact_id: str
    document_type: DocumentType
    pages: list[PreviewPage]
    sandbox: SandboxReport
