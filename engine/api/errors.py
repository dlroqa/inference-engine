"""OpenAI-compatible error payloads and exception handlers.

All error responses use the OpenAI error envelope so SDK clients parse them
natively::

    {"error": {"message": ..., "type": ..., "param": ..., "code": ...}}
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from engine.inference.types import (
    BackendBusyError,
    BackendNotReadyError,
    GenerationFailedError,
)
from engine.logging_setup import get_logger
from engine.telemetry.taxonomy import ErrorCategory, classify

_log = get_logger("engine.request")


def log_request_error(
    request: Request,
    *,
    category: ErrorCategory,
    stage: str,
    detail: str,
    status_code: int,
    exc: BaseException | None = None,
    with_stack: bool = False,
) -> None:
    """Emit a structured, request-correlated error log record.

    The record carries the error category, the lifecycle stage, the route, and the
    request id (from ``request.state`` when the edge assigned one) so an operator
    can see *whose* failure it was and *where* it happened. ``detail`` is one of
    the engine's own messages — never a prompt, response, or secret.
    """
    request_id = getattr(request.state, "request_id", None)
    log_fn = _log.error if status_code >= 500 else _log.warning
    log_fn(
        "request_error",
        extra={
            "request_id": request_id,
            "route": request.url.path,
            "category": category.value,
            "stage": stage,
            "detail": detail,
            "status": status_code,
        },
        exc_info=exc if with_stack else None,
    )


class OpenAIError(Exception):
    """An error the API edge raises to produce an OpenAI-shaped response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        type: str = "invalid_request_error",
        param: str | None = None,
        code: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.type = type
        self.param = param
        self.code = code
        self.headers = headers


def error_response(
    message: str,
    *,
    status_code: int,
    type: str = "invalid_request_error",
    param: str | None = None,
    code: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": type, "param": param, "code": code}},
        headers=headers,
    )


class AnthropicError(Exception):
    """An error the Anthropic edge raises to produce an Anthropic-shaped response.

    Anthropic uses ``{"type": "error", "error": {"type", "message"}}`` with its own
    ``type`` vocabulary (``invalid_request_error``, ``authentication_error``,
    ``not_found_error``, ``rate_limit_error``, ``overloaded_error``, ``api_error``).
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        type: str = "invalid_request_error",
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.type = type
        self.headers = headers


def anthropic_error_response(
    message: str,
    *,
    status_code: int,
    type: str = "invalid_request_error",
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"type": "error", "error": {"type": type, "message": message}},
        headers=headers,
    )


def _with_request_id(
    request: Request, headers: dict[str, str] | None = None
) -> dict[str, str] | None:
    """Attach the correlation id to an error response so failures are traceable."""
    request_id = getattr(request.state, "request_id", None)
    if request_id is None:
        return headers
    merged = dict(headers or {})
    merged.setdefault("x-request-id", request_id)
    return merged


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AnthropicError)
    async def _handle_anthropic_error(request: Request, exc: AnthropicError) -> JSONResponse:
        log_request_error(
            request,
            category=classify(exc, status_code=exc.status_code),
            stage="edge",
            detail=exc.message,
            status_code=exc.status_code,
        )
        return anthropic_error_response(
            exc.message,
            status_code=exc.status_code,
            type=exc.type,
            headers=_with_request_id(request, exc.headers),
        )

    @app.exception_handler(OpenAIError)
    async def _handle_openai_error(request: Request, exc: OpenAIError) -> JSONResponse:
        log_request_error(
            request,
            category=classify(exc, status_code=exc.status_code, code=exc.code),
            stage="edge",
            detail=exc.message,
            status_code=exc.status_code,
        )
        return error_response(
            exc.message,
            status_code=exc.status_code,
            type=exc.type,
            param=exc.param,
            code=exc.code,
            headers=_with_request_id(request, exc.headers),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = exc.errors()
        first = errors[0] if errors else {}
        loc = first.get("loc", ())
        # Drop the leading "body" segment for a cleaner param path.
        param = ".".join(str(p) for p in loc[1:]) or None
        msg = first.get("msg", "invalid request")
        detail = f"{param}: {msg}" if param else msg
        log_request_error(
            request,
            category=ErrorCategory.VALIDATION,
            stage="validation",
            detail=detail,
            status_code=400,
        )
        # Return the error in the dialect the caller expects.
        if request.url.path.startswith("/v1/messages"):
            return anthropic_error_response(
                detail,
                status_code=400,
                type="invalid_request_error",
                headers=_with_request_id(request),
            )
        return error_response(
            detail,
            status_code=400,
            type="invalid_request_error",
            param=param,
            headers=_with_request_id(request),
        )

    @app.exception_handler(BackendNotReadyError)
    async def _handle_not_ready(request: Request, exc: BackendNotReadyError) -> JSONResponse:
        detail = str(exc) or "no model is loaded"
        log_request_error(
            request,
            category=ErrorCategory.MODEL,
            stage="backend",
            detail=detail,
            status_code=503,
        )
        if request.url.path.startswith("/v1/messages"):
            return anthropic_error_response(
                detail, status_code=503, type="api_error", headers=_with_request_id(request)
            )
        return error_response(
            detail,
            status_code=503,
            type="service_unavailable",
            code="model_not_loaded",
            headers=_with_request_id(request),
        )

    @app.exception_handler(BackendBusyError)
    async def _handle_busy(request: Request, exc: BackendBusyError) -> JSONResponse:
        detail = str(exc) or "the model is busy"
        log_request_error(
            request,
            category=ErrorCategory.BACKEND,
            stage="backend",
            detail=detail,
            status_code=503,
        )
        if request.url.path.startswith("/v1/messages"):
            return anthropic_error_response(
                detail, status_code=503, type="api_error", headers=_with_request_id(request)
            )
        return error_response(
            detail,
            status_code=503,
            type="service_unavailable",
            code="model_busy",
            headers=_with_request_id(request),
        )

    @app.exception_handler(GenerationFailedError)
    async def _handle_gen_failed(request: Request, exc: GenerationFailedError) -> JSONResponse:
        detail = str(exc) or "generation failed"
        log_request_error(
            request,
            category=ErrorCategory.BACKEND,
            stage="generation",
            detail=detail,
            status_code=500,
            exc=exc,
            with_stack=True,
        )
        if request.url.path.startswith("/v1/messages"):
            return anthropic_error_response(
                detail, status_code=500, type="api_error", headers=_with_request_id(request)
            )
        return error_response(
            detail,
            status_code=500,
            type="server_error",
            headers=_with_request_id(request),
        )
