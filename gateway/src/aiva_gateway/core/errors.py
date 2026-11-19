"""Structured error codes (LLD §19). Denials carry a rule ID and remediation without policy internals."""

from typing import Any


class AivaError(Exception):
    status_code = 500
    code = "INTERNAL_ERROR"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        remediation: str | None = None,
        rule_ids: list[str] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.remediation = remediation
        self.rule_ids = rule_ids or []
        self.details = details or {}


class AuthenticationError(AivaError):
    status_code = 401
    code = "AUTHENTICATION_REQUIRED"


class PermissionDeniedError(AivaError):
    status_code = 403
    code = "PERMISSION_DENIED"


class NotFoundError(AivaError):
    status_code = 404
    code = "NOT_FOUND"


class ConflictError(AivaError):
    status_code = 409
    code = "CONFLICT"


class ValidationFailedError(AivaError):
    status_code = 422
    code = "VALIDATION_FAILED"


class NotImplementedYetError(AivaError):
    status_code = 501
    code = "NOT_IMPLEMENTED"


class DependencyUnavailableError(AivaError):
    """A dependency needed to decide safely is down; the gateway fails closed."""

    status_code = 503
    code = "DEPENDENCY_UNAVAILABLE"
    retryable = True
