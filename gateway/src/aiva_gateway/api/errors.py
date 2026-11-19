"""Uniform error envelope for all routes; the OpenAI-compatible routes use OpenAI's (ADR-0026)."""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from aiva_gateway.contracts.common import ErrorBody, ErrorEnvelope
from aiva_gateway.contracts.openai_compat import AivaErrorInfo, OpenAIError, OpenAIErrorEnvelope
from aiva_gateway.core.errors import AivaError

# The OpenAI-compatible routes: their errors take OpenAI's shape, so any OpenAI SDK reads them.
OPENAI_PATHS = ("/v1/chat/completions", "/v1/models")


def error_response(status_code: int, body: ErrorBody, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content=ErrorEnvelope(error=body).model_dump(mode="json"), headers=headers
    )


def is_openai_path(request: Request) -> bool:
    path = request.url.path.rstrip("/")
    return path in OPENAI_PATHS or path.startswith("/v1/models/")


def openai_error_type(status_code: int) -> str:
    """OpenAI's error `type` for an HTTP status."""
    if status_code == 401:
        return "authentication_error"
    if status_code == 403:
        return "permission_error"
    if status_code == 404:
        return "not_found_error"
    if status_code == 429:
        return "rate_limit_error"
    if status_code >= 500:
        return "api_error"
    return "invalid_request_error"


def openai_error(
    status_code: int,
    code: str,
    message: str,
    *,
    param: str | None = None,
    aiva: AivaErrorInfo | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    envelope = OpenAIErrorEnvelope(
        error=OpenAIError(message=message, type=openai_error_type(status_code), param=param, code=code),
        aiva=aiva,
    )
    return JSONResponse(
        status_code=status_code, content=envelope.model_dump(mode="json", exclude_none=True), headers=headers
    )


def _validation_param(error: dict[str, Any]) -> str:
    return ".".join(str(part) for part in error.get("loc", ())[1:]) or "body"


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AivaError)
    async def _aiva_error(request: Request, exc: AivaError) -> JSONResponse:
        retry_after = getattr(exc, "retry_after", None)
        headers = {"Retry-After": str(retry_after)} if isinstance(retry_after, int) else None
        if is_openai_path(request):
            info = AivaErrorInfo(
                remediation=exc.remediation,
                rule_ids=exc.rule_ids,
                retryable=exc.retryable,
                details=exc.details,
            )
            return openai_error(exc.status_code, exc.code.lower(), exc.message, aiva=info, headers=headers)
        return error_response(
            exc.status_code,
            ErrorBody(
                code=exc.code,
                message=exc.message,
                remediation=exc.remediation,
                rule_ids=exc.rule_ids,
                retryable=exc.retryable,
                details=exc.details,
            ),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", "")} for e in exc.errors()]
        if is_openai_path(request):
            # OpenAI answers an invalid request with 400 and names the first offending parameter.
            first = exc.errors()[0] if exc.errors() else {}
            param = _validation_param(first)
            if first.get("type") == "extra_forbidden":
                return openai_error(
                    400, "unsupported_parameter", f"Unsupported parameter: '{param}'.", param=param
                )
            return openai_error(
                400, "invalid_value", f"Invalid '{param}': {first.get('msg', '')}.", param=param
            )
        return error_response(
            422,
            ErrorBody(
                code="VALIDATION_FAILED", message="The request is invalid.", details={"errors": errors}
            ),
        )


ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorEnvelope, "description": "Missing or invalid credentials."},
    403: {"model": ErrorEnvelope, "description": "Authenticated but not permitted."},
    422: {"model": ErrorEnvelope, "description": "Invalid request."},
}
