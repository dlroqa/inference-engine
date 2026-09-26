"""Model lifecycle API for the dashboard (Block 6).

Operator-gated endpoints to import/download GGUF models, watch download progress,
and load/unload/delete them from the registry:

- ``GET    /admin/models``            — list registry models (+ host compat).
- ``POST   /admin/models/import``     — register a local GGUF file (checksum-verified).
- ``POST   /admin/models/download``   — start a checksum-verified download (HF or URL).
- ``GET    /admin/models/{id}``       — one model (poll download progress).
- ``POST   /admin/models/{id}/cancel``— cancel an in-progress download.
- ``POST   /admin/models/{id}/load``  — load the model (becomes the active model).
- ``POST   /admin/models/{id}/unload``— unload the active model.
- ``DELETE /admin/models/{id}``       — remove a model (unload it first).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from engine.api.deps import operator_identity, require_operator
from engine.api.errors import OpenAIError
from engine.inference.base import InferenceBackend
from engine.inference.types import BackendError, BackendState
from engine.models.registry import ModelRecord, ModelRegistry, ModelStatus
from engine.models.service import ModelService, ModelServiceError

router = APIRouter(prefix="/admin/models", tags=["models"])

_LOADED = (BackendState.READY, BackendState.GENERATING)


def _require_management(request: Request) -> None:
    """Kill switch: dynamic model management (import/download/load/unload/delete)."""
    if not request.app.state.settings.allow_model_management:
        raise OpenAIError(
            "model management is disabled by the operator",
            status_code=403,
            type="invalid_request_error",
            code="model_management_disabled",
        )


def _require_downloads(request: Request) -> None:
    """Kill switch / egress policy: fetching models from the network."""
    if not request.app.state.settings.allow_network_downloads:
        raise OpenAIError(
            "network model downloads are disabled by the operator",
            status_code=403,
            type="invalid_request_error",
            code="downloads_disabled",
        )


def _audit(request: Request, action: str, target: str | None, **detail: Any) -> None:
    request.app.state.audit.record(
        action, actor=operator_identity(request), target=target, detail=detail
    )


def _service(request: Request) -> ModelService:
    return request.app.state.model_service


def _registry(request: Request) -> ModelRegistry:
    return request.app.state.model_registry


def _is_loaded(request: Request, record: ModelRecord) -> bool:
    backend: InferenceBackend | None = getattr(request.app.state, "backend", None)
    return (
        record.active
        and backend is not None
        and backend.state in _LOADED
        and backend.capabilities().model_id == record.name
    )


_SENSITIVE_QUERY_HINTS = ("token", "key", "secret", "sig", "auth", "password", "credential")
_MASK = "***"

# A URL inside free-form text (an exception message): scheme://... up to
# whitespace, a quote or a bracket.
_URL_IN_TEXT = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s'\"<>()\[\]{}]+")
# Userinfo right after "://" (``user:pass@``), for the conservative fallback.
_USERINFO = re.compile(r"(?<=://)[^/?#\s]*@")
# A query parameter anywhere in text, e.g. a request target without a scheme
# (``/m.gguf?token=abc``) as some HTTP errors report it.
_QUERY_PARAM = re.compile(r"([?&])([^=&#\s'\"<>]+)=([^&#\s'\"<>]*)")


def _is_sensitive(name: str) -> bool:
    return any(h in name.lower() for h in _SENSITIVE_QUERY_HINTS)


def _conservative_redact(url: str) -> str:
    """Redaction when a URL cannot be parsed: drop userinfo and the whole query."""
    url = _USERINFO.sub("", url.split("#", 1)[0])
    base, sep, _ = url.partition("?")
    return f"{base}?{_MASK}" if sep else base


def redact_source_ref(source_ref: str | None) -> str | None:
    """Strip credentials from a model's recorded source before it leaves the API.

    Removes URL userinfo (``user:pass@``) and masks query parameters whose names
    look credential-bearing (``?token=``, ``X-Amz-Signature`` ...). Hugging Face
    ``repo/file`` references and plain paths pass through unchanged. A URL that
    cannot be parsed (e.g. a malformed port) is redacted conservatively instead
    of failing the request.
    """
    if not source_ref or "://" not in source_ref:
        return source_ref
    try:
        parts = urlsplit(source_ref)
        netloc = parts.hostname or ""
        if parts.port is not None:
            netloc = f"{netloc}:{parts.port}"
        query = [
            (k, _MASK if _is_sensitive(k) else v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
        ]
        return urlunsplit((parts.scheme, netloc, parts.path, urlencode(query, safe="*"), ""))
    except ValueError:
        return _conservative_redact(source_ref)


def redact_urls_in_text(text: str | None) -> str | None:
    """Redact credentials from every URL and query parameter in free-form text.

    Used for the model ``error`` string, which records raw exception messages
    that can quote the download URL, a redirect target, or a request target.
    Uses the same sensitive-name policy as :func:`redact_source_ref`. Anything
    that is not credential-bearing is kept, so the error stays useful. If the
    text cannot be processed at all, it is withheld rather than returned raw.
    """
    if not text:
        return text

    def url(match: re.Match[str]) -> str:
        found = match.group(0)
        trail = ""
        while found and found[-1] in ".,;:!":
            trail = found[-1] + trail
            found = found[:-1]
        return (redact_source_ref(found) or "") + trail

    def param(match: re.Match[str]) -> str:
        sep, name, value = match.groups()
        return f"{sep}{name}={_MASK if _is_sensitive(name) and value else value}"

    try:
        return _QUERY_PARAM.sub(param, _URL_IN_TEXT.sub(url, text))
    except Exception:  # never return unredacted text
        return "error details withheld (they could not be redacted safely)"


def _serialize(request: Request, record: ModelRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "name": record.name,
        "filename": record.filename,
        "source_type": record.source_type,
        "source_ref": redact_source_ref(record.source_ref),
        "sha256": record.sha256,
        "size_bytes": record.size_bytes,
        "downloaded_bytes": record.downloaded_bytes,
        "progress": record.progress,
        "quant": record.quant,
        "arch": record.arch,
        "context_length": record.context_length,
        "status": record.status,
        "error": redact_urls_in_text(record.error),
        "active": record.active,
        "loaded": _is_loaded(request, record),
        "added_at": record.added_at,
        "compat": _service(request).compat_for(record),
    }


def _require(request: Request, model_id: str) -> ModelRecord:
    record = _registry(request).get(model_id)
    if record is None:
        raise OpenAIError(
            f"model {model_id!r} not found",
            status_code=404,
            type="invalid_request_error",
            code="model_not_found",
        )
    return record


class ImportBody(BaseModel):
    model_config = {"extra": "forbid"}
    path: str
    name: str | None = Field(default=None, max_length=200)


class DownloadBody(BaseModel):
    model_config = {"extra": "forbid"}
    source_type: Literal["huggingface", "url"]
    name: str | None = Field(default=None, max_length=200)
    repo: str | None = None
    filename: str | None = None
    revision: str = "main"
    url: str | None = None
    expected_sha256: str | None = None


@router.get("")
def list_models(request: Request) -> dict[str, Any]:
    require_operator(request)
    return {"models": [_serialize(request, r) for r in _registry(request).list()]}


@router.get("/{model_id}")
def get_model(request: Request, model_id: str) -> dict[str, Any]:
    require_operator(request)
    return _serialize(request, _require(request, model_id))


@router.post("/import")
async def import_model(request: Request, body: ImportBody) -> dict[str, Any]:
    require_operator(request)
    _require_management(request)
    try:
        record = await _service(request).import_local(Path(body.path), body.name)
    except ModelServiceError as exc:
        raise OpenAIError(
            str(exc), status_code=400, type="invalid_request_error", code="model_import_failed"
        ) from exc
    _audit(request, "model.import", record.id, name=record.name, source="import")
    return _serialize(request, record)


@router.post("/download")
async def download_model(request: Request, body: DownloadBody) -> dict[str, Any]:
    # Async so the background download task is created on the running event loop.
    require_operator(request)
    _require_management(request)
    _require_downloads(request)
    try:
        record = _service(request).start_download(
            source_type=body.source_type,
            name=body.name,
            repo=body.repo,
            filename=body.filename,
            revision=body.revision,
            url=body.url,
            expected_sha256=body.expected_sha256,
        )
    except ModelServiceError as exc:
        raise OpenAIError(
            str(exc), status_code=400, type="invalid_request_error", code="model_download_failed"
        ) from exc
    _audit(request, "model.download", record.id, name=record.name, source=body.source_type)
    return _serialize(request, record)


@router.post("/{model_id}/cancel")
def cancel_download(request: Request, model_id: str) -> dict[str, Any]:
    require_operator(request)
    record = _require(request, model_id)
    if record.status != ModelStatus.DOWNLOADING.value:
        raise OpenAIError(
            "model is not downloading",
            status_code=409,
            type="invalid_request_error",
            code="not_downloading",
        )
    _service(request).cancel(model_id)
    return {"cancelling": True, "id": model_id}


@router.post("/{model_id}/load")
async def load_model(request: Request, model_id: str) -> dict[str, Any]:
    require_operator(request)
    _require_management(request)
    record = _require(request, model_id)
    if record.status != ModelStatus.READY.value:
        raise OpenAIError(
            f"model is not ready to load (status: {record.status})",
            status_code=409,
            type="invalid_request_error",
            code="model_not_ready",
        )
    # Loading makes the primary serve ``record.name`` as its client-facing model id
    # (Block 12.1). Reject a load that would collide with a configured virtual model
    # name, rather than let virtual routing silently shadow a physical model.
    router = getattr(request.app.state, "router", None)
    if router is not None and record.name in router.virtual_model_names():
        raise OpenAIError(
            f"cannot load model {record.name!r}: its id collides with a configured "
            "virtual model name",
            status_code=409,
            type="invalid_request_error",
            code="model_name_conflict",
        )
    service = _service(request)
    backend = service.build_backend(record)
    try:
        await backend.load()
    except BackendError as exc:
        raise OpenAIError(
            f"model failed to load: {exc}",
            status_code=503,
            type="service_unavailable",
            code="model_load_failed",
        ) from exc

    previous: InferenceBackend | None = getattr(request.app.state, "backend", None)
    request.app.state.backend = backend
    _registry(request).set_active(model_id)
    if previous is not None and previous is not backend:
        try:
            await previous.unload()
        except BackendError:  # pragma: no cover - best-effort cleanup
            pass
    _audit(request, "model.load", model_id, name=record.name)
    return {"result": "loaded", "model": _serialize(request, _require(request, model_id))}


@router.post("/{model_id}/unload")
async def unload_model(request: Request, model_id: str) -> dict[str, Any]:
    require_operator(request)
    _require_management(request)
    record = _require(request, model_id)
    backend: InferenceBackend | None = getattr(request.app.state, "backend", None)
    if _is_loaded(request, record) and backend is not None:
        await backend.unload()
        request.app.state.backend = None
    _registry(request).clear_active()
    _audit(request, "model.unload", model_id, name=record.name)
    return {"result": "unloaded", "model": _serialize(request, _require(request, model_id))}


@router.delete("/{model_id}")
async def delete_model(request: Request, model_id: str) -> dict[str, Any]:
    require_operator(request)
    _require_management(request)
    record = _require(request, model_id)
    if _is_loaded(request, record):
        raise OpenAIError(
            "unload the model before deleting it",
            status_code=409,
            type="invalid_request_error",
            code="model_loaded",
        )
    _service(request).cancel(model_id)  # stop any in-flight download first
    service = _service(request)
    service.delete_files(record)
    _registry(request).delete(model_id)
    _audit(request, "model.delete", model_id, name=record.name)
    return {"deleted": True, "id": model_id}
