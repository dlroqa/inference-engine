"""Shared FastAPI dependencies for the operator-facing surfaces.

The operator gate is applied to every admin and operability endpoint (and, via
the same :meth:`Gateway.operator_access` decision, to the operator WebSockets),
so it lives here once rather than being re-implemented per router. It never
relies on the obscurity of a route.

Precedence (credentials are checked before the loopback exception):

- valid operator key -> allowed;
- valid billing-client key -> ``403 operator_role_required`` (even on loopback);
- invalid/revoked key, or no key while effective auth is on -> ``401
  operator_access_required`` (even on loopback);
- no key with effective auth off -> allowed for loopback development use only.
"""

from __future__ import annotations

from fastapi import Request

from engine.api.errors import OpenAIError
from engine.gateway import LOCAL_KEY_ID, Gateway, OperatorAccess, OperatorDecision, extract_token


def operator_decision(request: Request) -> OperatorDecision:
    """Evaluate the operator gate for an HTTP request (no exception raised)."""
    gateway: Gateway = request.app.state.gateway
    host = request.client.host if request.client else None
    return gateway.operator_access(client_host=host, token=extract_token(request))


def operator_identity(request: Request) -> str:
    """Best-effort identity of the operator making an admin call, for audit records.

    Returns the API key id when a valid key is presented, else ``"local"`` (a
    loopback dev client). Never returns a secret.
    """
    gateway: Gateway = request.app.state.gateway
    token = extract_token(request)
    if token:
        record = gateway.keys.verify(token)
        if record is not None:
            return record.id
    return LOCAL_KEY_ID


def require_operator(request: Request) -> OperatorDecision:
    """Raise ``401``/``403`` unless the caller passes the operator gate."""
    decision = operator_decision(request)
    if decision.access is OperatorAccess.FORBIDDEN_ROLE:
        raise OpenAIError(
            "this key belongs to a client and cannot use operator endpoints",
            status_code=403,
            type="permission_error",
            code="operator_role_required",
        )
    if decision.access is not OperatorAccess.ALLOWED:
        raise OpenAIError(
            "operator access required",
            status_code=401,
            type="invalid_request_error",
            code="operator_access_required",
        )
    return decision
