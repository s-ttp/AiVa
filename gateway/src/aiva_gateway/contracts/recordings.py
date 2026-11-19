"""Recorded provider exchanges: a record-mode gateway's export (`GET /v1/admin/recordings`, ADR-0009)."""

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from aiva_gateway.contracts.common import ContractModel


class RecordedRequest(ContractModel):
    method: str
    url: str = Field(description="Without query credentials.")
    body: Any = Field(description="The JSON request body as sent (text if it was not JSON).")


class RecordedResponse(ContractModel):
    status_code: int
    content_type: str
    body: str = Field(description="The response body as received (a stream is its full text).")


class RecordingContent(ContractModel):
    """A recording file's content, exactly as replay reads it: bodies only, never headers or credentials."""

    request: RecordedRequest
    response: RecordedResponse


class RecordedExchange(ContractModel):
    name: str = Field(
        description="The file name replay looks up: `<label>-<fingerprint>.json`, label = the model or CIM."
    )
    content: RecordingContent


class RecordingBundle(ContractModel):
    """Every recording in the gateway's recordings directory. A file containing any of the gateway's
    configured secrets is withheld rather than exported."""

    inference_mode: Literal["record"]
    data_designation: str
    exported_at: datetime
    withheld: int = Field(
        ge=0, description="Files left out: not a recording, over 4 MB, or holding a secret."
    )
    recordings: list[RecordedExchange]
