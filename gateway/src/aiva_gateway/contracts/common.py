from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ErrorBody(ContractModel):
    code: str = Field(description="Stable machine-readable error code.")
    message: str
    remediation: str | None = Field(default=None, description="Actionable next step for the caller.")
    rule_ids: list[str] = Field(default_factory=list, description="Policy rule IDs behind a denial.")
    retryable: bool = False
    run_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(ContractModel):
    error: ErrorBody


class PageInfo(ContractModel):
    next_cursor: str | None = None
    limit: int
