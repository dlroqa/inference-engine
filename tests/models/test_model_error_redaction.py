"""Model API responses never echo credentials from a model's source or error (A2c).

The registry stores raw exception text in ``error`` (e.g. a failed download can
quote its URL, a redirect target or a request target). List and detail both
serialize through the same redaction. All secrets here are synthetic.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine.api.models_router import redact_source_ref, redact_urls_in_text
from engine.config import Settings
from engine.main import create_app
from engine.models.registry import ModelStatus

LOOPBACK = ("127.0.0.1", 40000)
SECRETS = ("s3cr3t-pass", "tok-abc123", "sig-deadbeef", "redir-key-999", "pw-malformed")


@pytest.fixture
def client(tmp_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(tmp_settings), client=LOOPBACK) as c:
        yield c


def _seed(
    client: TestClient, tmp_path: Path, *, source_type: str, source_ref: str, error: str
) -> str:
    registry = client.app.state.model_registry  # type: ignore[attr-defined]
    record = registry.create(
        name=f"m-{source_type}",
        filename="m.gguf",
        path=tmp_path / "m.gguf",
        source_type=source_type,
        source_ref=source_ref,
        status=ModelStatus.ERROR,
    )
    registry.update(record.id, error=error)
    return record.id


CASES = {
    "exact source URL": (
        "https://bob:s3cr3t-pass@cdn.example.com/m.gguf?token=tok-abc123&v=2",
        "HTTP 403 fetching "
        "https://bob:s3cr3t-pass@cdn.example.com/m.gguf?token=tok-abc123&v=2.",
    ),
    "different redirect URL": (
        "https://cdn.example.com/m.gguf",
        "redirected to "
        "https://s3.example.com/b/m.gguf?X-Amz-Signature=sig-deadbeef&X-Amz-Date=20260926",
    ),
    "request target without a scheme": (
        "https://cdn.example.com/m.gguf?api_key=redir-key-999",
        "URL can't contain control characters. "
        "'/m.gguf?api_key=redir-key-999 x' (found at least ' ')",
    ),
    "multiple URLs and userinfo": (
        "http://cdn.example.com/m.gguf",
        "tried http://u:s3cr3t-pass@a.example/m.gguf "
        "then HTTPS://b.example/m.gguf?Signature=sig-deadbeef",
    ),
    "malformed port": (
        "https://u:pw-malformed@cdn.example.com:notaport/m.gguf?token=tok-abc123",
        "bad URL https://u:pw-malformed@cdn.example.com:notaport/m.gguf?token=tok-abc123",
    ),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_list_and_detail_redact_source_and_error(
    client: TestClient, tmp_path: Path, case: str
) -> None:
    source_ref, error = CASES[case]
    model_id = _seed(client, tmp_path, source_type="url", source_ref=source_ref, error=error)

    detail = client.get(f"/admin/models/{model_id}")
    listed = client.get("/admin/models")
    assert detail.status_code == 200
    assert listed.status_code == 200
    for body in (detail.text, listed.text):
        for secret in SECRETS:
            assert secret not in body, f"{case}: a credential is in the response"
    # The error remains useful: it is still there, and any host it named still is.
    redacted = detail.json()["error"]
    assert redacted
    if "://" in error:
        assert "example" in redacted


def test_non_sensitive_error_text_is_unchanged(client: TestClient, tmp_path: Path) -> None:
    error = "checksum mismatch: expected aaaa, got bbbb (see https://docs.example/verify?lang=en)"
    model_id = _seed(
        client, tmp_path, source_type="url", source_ref="https://x.example/m.gguf", error=error
    )
    assert client.get(f"/admin/models/{model_id}").json()["error"] == error


def test_plain_path_and_hugging_face_sources_are_unchanged(
    client: TestClient, tmp_path: Path
) -> None:
    hf = _seed(
        client, tmp_path, source_type="huggingface", source_ref="org/repo/m.gguf@main", error="boom"
    )
    body = client.get(f"/admin/models/{hf}").json()
    assert body["source_ref"] == "org/repo/m.gguf@main"
    assert body["error"] == "boom"
    local = _seed(
        client,
        tmp_path,
        source_type="import",
        source_ref="/srv/models/m.gguf",
        error="not a valid GGUF file",
    )
    assert client.get(f"/admin/models/{local}").json()["source_ref"] == "/srv/models/m.gguf"


def test_helpers_are_conservative_and_never_raise() -> None:
    assert redact_urls_in_text(None) is None
    assert redact_urls_in_text("") == ""
    assert redact_urls_in_text("no urls here") == "no urls here"
    # Malformed input: userinfo removed and the whole query masked.
    assert redact_source_ref("https://u:pw-malformed@h:bad/m?x=1") == "https://h:bad/m?***"
    assert redact_source_ref("http://[::1/m?token=tok-abc123") == "http://[::1/m?***"
    text = redact_urls_in_text("a https://h.example/m?token=tok-abc123, and ?secret=s3cr3t-pass;")
    assert text is not None
    assert "tok-abc123" not in text
    assert "s3cr3t-pass" not in text
    assert text.startswith("a https://h.example/m?token=***,")
