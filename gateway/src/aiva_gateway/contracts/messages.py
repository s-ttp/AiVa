"""Message, evidence and document-figure types shared by the chat, policy and artifact contracts and the
scenario catalog."""

from typing import Annotated, Literal

from pydantic import AfterValidator, Field

from aiva_gateway.contracts.common import ContractModel


class ChatMessage(ContractModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = Field(max_length=200_000)


class EvidenceItem(ContractModel):
    """Caller-supplied evidence. Treated as untrusted data, never as instructions (LLD §17)."""

    id: str = Field(max_length=200)
    text: str = Field(max_length=100_000)


def _unique_ids(items: list[EvidenceItem]) -> list[EvidenceItem]:
    """Packages, citations and the Context Guard address passages by id: one id, one passage."""
    seen: set[str] = set()
    repeated = []
    for index, item in enumerate(items):
        if item.id in seen:
            repeated.append(f"evidence[{index}]")  # positions, never the caller's free-form ids
        seen.add(item.id)
    if repeated:
        raise ValueError(f"evidence ids must be unique; repeated at {', '.join(repeated[:5])}")
    return items


EvidenceList = Annotated[list[EvidenceItem], AfterValidator(_unique_ids)]


class TestProfile(ContractModel):
    """Test Console simulation. Accepted only for users with the tester role (ADR-0007)."""

    __test__ = False  # not a pytest test class

    application_id: str
    simulated_source_ip: str | None = Field(
        default=None, description="Simulated client IP; recorded as source_mode=simulated."
    )


DocumentType = Literal["docx", "pptx"]


class CriticalFigure(ContractModel):
    """A figure the document must carry exactly as its source states it (LLD §15 #10)."""

    id: str = Field(max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    label: str = Field(max_length=200)
    value: str = Field(
        max_length=64, description="The number exactly as written in the source, e.g. '1,284.5'."
    )
    unit: str | None = Field(default=None, max_length=32, description="e.g. 'AED m', '%'.")
    source_id: str = Field(max_length=200, description="ID of the evidence item that states the figure.")
