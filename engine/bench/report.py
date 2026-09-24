"""Versioned, pure benchmark report (Block 12.2b).

``RunReport`` bundles the identity stamp, the safe workload manifest, the client
metrics, and explicit source/availability metadata for every metric that cannot be
derived from client samples (token/cost/output-TPS come only from an aggregate
``/admin/routes`` delta under an exclusive-target run).

This module is **pure**: ``to_dict``/``from_dict``/JSON round-trips touch no clock,
environment, platform, network, or Git. Runtime identity is collected at the CLI/
driver boundary and injected as an :class:`IdentityStamp`. Reports never contain
prompts, response content, session identifiers, credentials, authorization headers,
raw request bodies, or unbounded per-request samples.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from engine.bench import HARNESS_VERSION, SCHEMA_VERSION


def sanitize_origin(url: str) -> str:
    """Reduce a base URL to ``scheme://host[:port]`` — no userinfo/path/query/fragment.

    Credentials in userinfo (``https://user:pass@host``) and any query/fragment are
    dropped so an origin can be recorded without persisting secrets.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "unknown"
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    if not parts.scheme or not host:
        return "unknown"
    return urlunsplit((parts.scheme, host, "", "", ""))


@dataclass(frozen=True)
class IdentityStamp:
    """Reproducibility identity — collected at the boundary, injected here (pure)."""

    harness_version: str
    build_info: dict[str, object]  # engine.buildinfo.build_info() output (commit may be None)
    python_version: str
    platform: str
    timestamp: str  # ISO-8601, collected by the caller
    origin: str  # sanitized base URL (no credentials)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IdentityStamp:
        return cls(
            harness_version=str(data["harness_version"]),
            build_info=dict(data.get("build_info", {})),
            python_version=str(data["python_version"]),
            platform=str(data["platform"]),
            timestamp=str(data["timestamp"]),
            origin=str(data["origin"]),
        )


class ReportError(ValueError):
    """A report is malformed or its schema version is incompatible."""


@dataclass(frozen=True)
class RunReport:
    """A full benchmark run: identity + manifest + results + metric sources."""

    identity: IdentityStamp
    manifest: dict[str, object]
    results: dict[str, object]  # engine.bench.metrics.aggregate(...) output
    #: Aggregate (model, backend) route-snapshot delta, or None when unavailable.
    #: Labelled with its source and the exclusive-target flag; never per-slice.
    route_delta: dict[str, object] | None = None
    metric_sources: dict[str, str] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    harness_version: str = HARNESS_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "harness_version": self.harness_version,
            "identity": self.identity.to_dict(),
            "manifest": self.manifest,
            "results": self.results,
            "route_delta": self.route_delta,
            "metric_sources": self.metric_sources,
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunReport:
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise ReportError(
                f"incompatible report schema_version {version!r}; this harness reads "
                f"{SCHEMA_VERSION}"
            )
        try:
            identity = IdentityStamp.from_dict(data["identity"])
            manifest = dict(data["manifest"])
            results = dict(data["results"])
        except (KeyError, TypeError) as exc:
            raise ReportError(f"malformed report: {exc}") from exc
        route_delta = data.get("route_delta")
        return cls(
            identity=identity,
            manifest=manifest,
            results=results,
            route_delta=dict(route_delta) if isinstance(route_delta, dict) else None,
            metric_sources=dict(data.get("metric_sources", {})),
            schema_version=int(version),
            harness_version=str(data.get("harness_version", "")),
        )

    @classmethod
    def from_json(cls, text: str) -> RunReport:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ReportError(f"invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ReportError("report must be a JSON object")
        return cls.from_dict(data)
