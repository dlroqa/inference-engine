"""Pure report round-trip, schema validation, and privacy (Block 12.2b)."""

from __future__ import annotations

import pytest

from engine.bench import HARNESS_VERSION, SCHEMA_VERSION
from engine.bench.report import IdentityStamp, ReportError, RunReport, sanitize_origin


def _report() -> RunReport:
    return RunReport(
        identity=IdentityStamp(
            harness_version=HARNESS_VERSION,
            build_info={"version": "0.0.0", "commit": None, "built_at": None},
            python_version="3.12.1",
            platform="linux (x86_64)",
            timestamp="2026-09-24T00:00:00+00:00",
            origin="https://gateway.example",
        ),
        manifest={"name": "smoke", "seed": 7, "counts": {"unique": 2}},
        results={"overall": {"admitted": 2, "error_rate": 0.0}},
        metric_sources={"ttft": "client"},
    )


def test_json_round_trip() -> None:
    original = _report()
    restored = RunReport.from_json(original.to_json())
    assert restored.to_dict() == original.to_dict()
    assert restored.schema_version == SCHEMA_VERSION
    assert restored.identity.python_version == "3.12.1"


def test_rejects_incompatible_schema() -> None:
    data = _report().to_dict()
    data["schema_version"] = SCHEMA_VERSION + 1
    with pytest.raises(ReportError, match="schema_version"):
        RunReport.from_dict(data)


def test_rejects_malformed_and_non_object() -> None:
    with pytest.raises(ReportError):
        RunReport.from_json("not json")
    with pytest.raises(ReportError):
        RunReport.from_json("[]")  # not an object
    bad = _report().to_dict()
    del bad["identity"]
    with pytest.raises(ReportError, match="malformed"):
        RunReport.from_dict(bad)


def test_identity_fields_present() -> None:
    d = _report().to_dict()["identity"]
    for key in (
        "harness_version",
        "build_info",
        "python_version",
        "platform",
        "timestamp",
        "origin",
    ):
        assert key in d


def test_sanitize_origin_strips_credentials_and_extras() -> None:
    origin = sanitize_origin("https://user:SECRET@gateway.example:8443/v1?token=APIKEY#frag")
    assert origin == "https://gateway.example:8443"
    assert "SECRET" not in origin and "APIKEY" not in origin and "user" not in origin
    assert sanitize_origin("not a url") == "unknown"


def test_report_contains_no_credential_or_prompt_markers() -> None:
    # A report built from safe inputs never carries prompt/credential markers even
    # if the origin URL contained them.
    report = RunReport(
        identity=IdentityStamp(
            harness_version=HARNESS_VERSION,
            build_info={},
            python_version="3.12",
            platform="t",
            timestamp="2026-09-24T00:00:00+00:00",
            origin=sanitize_origin("https://user:SECRETKEY@host/v1?k=PROMPTMARKER"),
        ),
        manifest={"name": "smoke"},
        results={},
    )
    blob = report.to_json()
    assert "SECRETKEY" not in blob and "PROMPTMARKER" not in blob
