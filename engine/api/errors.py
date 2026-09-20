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


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(OpenAIError)
    async def _handle_openai_error(_: Request, exc: OpenAIError) -> JSONResponse:
        return error_response(
            exc.message,
            status_code=exc.status_code,
            type=exc.type,
            param=exc.param,
            code=exc.code,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = exc.errors()
        first = errors[0] if errors else {}
        loc = first.get("loc", ())
        # Drop the leading "body" segment for a cleaner param path.
        param = ".".join(str(p) for p in loc[1:]) or None
        msg = first.get("msg", "invalid request")
        detail = f"{param}: {msg}" if param else msg
        return error_response(detail, status_code=400, type="invalid_request_error", param=param)

    @app.exception_handler(BackendNotReadyError)
    async def _handle_not_ready(_: Request, exc: BackendNotReadyError) -> JSONResponse:
        return error_response(
            str(exc) or "no model is loaded",
            status_code=503,
            type="service_unavailable",
            code="model_not_loaded",
        )

    @app.exception_handler(BackendBusyError)
    async def _handle_busy(_: Request, exc: BackendBusyError) -> JSONResponse:
        return error_response(
            str(exc) or "the model is busy",
            status_code=503,
            type="service_unavailable",
            code="model_busy",
        )

    @app.exception_handler(GenerationFailedError)
    async def _handle_gen_failed(_: Request, exc: GenerationFailedError) -> JSONResponse:
        return error_response(
            str(exc) or "generation failed",
            status_code=500,
            type="server_error",
        )
