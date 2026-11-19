"""Session contracts: who the caller is (`GET /v1/auth/me`), the passcode sign-in of deployed gateways
(ADR-0019) and the local-only dev sign-in (ADR-0010)."""

from datetime import datetime
from typing import Literal

from pydantic import Field, SecretStr

from aiva_gateway.contracts.common import ContractModel
from aiva_gateway.core.enums import Role


class CallerProfile(ContractModel):
    """The authenticated caller. Clients derive navigation from `permissions`, never from token claims."""

    kind: Literal["application", "user"]
    tenant_id: str
    user_id: str | None = None
    display_name: str | None = None
    application_id: str | None = None
    department_id: str | None = None
    roles: list[Role] = Field(default_factory=list)
    permissions: list[str] = Field(
        default_factory=list, description="Permissions granted to a user's roles; empty for applications."
    )


class DemoUser(ContractModel):
    user_id: str
    display_name: str
    roles: list[Role]


class DevTokenRequest(ContractModel):
    user_id: str = Field(max_length=64)


class DemoTokenRequest(ContractModel):
    user_id: str = Field(max_length=64)
    passcode: SecretStr = Field(min_length=1, max_length=256, description="The demo passcode; never echoed.")


class PublicSession(ContractModel):
    """A public demo session (ADR-0019): a two-hour token for one pseudonymous visitor."""

    access_token: str
    token_type: Literal["Bearer"] = "Bearer"  # noqa: S105 - the scheme name, not a secret
    expires_at: datetime


class SignInOptions(ContractModel):
    """Which sign-ins this gateway offers to this request, so the UI shows only those."""

    public: bool = Field(description="A public demo session can be started (`POST /v1/auth/public/session`).")
    passcode: bool = Field(description="Passcode sign-in for the owner and demo users (`/v1/auth/demo/*`).")
    dev: bool = Field(description="Password-less dev sign-in (`/v1/auth/dev/*`); local and test only.")


class DevToken(ContractModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"  # noqa: S105 - the scheme name, not a secret
    expires_at: datetime
