"""Shared FastAPI dependencies for the operator-facing surfaces.

The operator gate (loopback development use *or* a valid API key) is applied to
every admin and operability endpoint, so it lives here once rather than being
re-implemented per router. It never relies on the obscurity of a route.
"""

from __future__ import annotations

from fastapi import Request

from engine.api.errors import OpenAIError
from engine.gateway import Gateway, extract_token


def require_operator(request: Request) -> None:
    """Raise ``401`` unless the caller is a loopback dev client or a valid key."""
    gateway: Gateway = request.app.state.gateway
    host = request.client.host if request.client else None
    if not gateway.operator_allowed(client_host=host, token=extract_token(request)):
        raise OpenAIError(
            "operator access required",
            status_code=401,
            type="invalid_request_error",
            code="operator_access_required",
        )
