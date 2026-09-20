"""Diagnostics bundle: redacted config, hardware summary, logs — no secrets."""

from __future__ import annotations

import json
from pathlib import Path

from engine.config import Settings
from engine.telemetry import diagnostics
from engine.telemetry.diagnostics import build_bundle, redact_config


class _FakeSettings:
    """Stand-in exposing ``model_dump`` with a secret-looking field to redact."""

    def model_dump(self, mode: str = "json") -> dict[str, object]:
        return {
            "host": "127.0.0.1",
            "api_key": "sk-should-be-masked",
            "webhook_secret": "shh",
            "port": 8000,
        }


def test_redactor_masks_secret_named_fields() -> None:
    redacted = redact_config(_FakeSettings())  # type: ignore[arg-type]
    assert redacted["api_key"] == "***"
    assert redacted["webhook_secret"] == "***"
    assert redacted["host"] == "127.0.0.1"
    assert redacted["port"] == 8000


def test_redact_real_settings_is_json_safe(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    redacted = redact_config(settings)
    # Fully serializable (paths stringified) and no live secrets present.
    json.dumps(redacted)
    assert "***" not in json.dumps(redacted)  # nothing secret-named in current config


def test_bundle_has_no_prompt_or_secret(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    snapshot = {"counters": {"requests_total": 1}, "energy": {"state": "unavailable"}}
    bundle = build_bundle(settings=settings, backend=None, collector=None, snapshot=snapshot)
    assert set(bundle) >= {"config", "hardware", "metrics", "recent_logs", "notes"}
    assert bundle["hardware"]["engine_version"] == diagnostics.__version__
    # The whole bundle must be JSON-serializable and secret-free by construction.
    text = json.dumps(bundle)
    assert "sk-" not in text
    assert bundle["metrics"]["energy"]["state"] == "unavailable"
