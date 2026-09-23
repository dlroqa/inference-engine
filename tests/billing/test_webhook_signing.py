"""Standard Webhooks signing/verification and rotation (Block 11.2)."""

from __future__ import annotations

from engine.billing.webhooks import signing


def test_sign_verify_roundtrip() -> None:
    secret = signing.generate_secret()
    body = '{"id":"evt_1","type":"x"}'
    header = signing.signature_header([secret], "msg_1", 1_000, body)
    assert header.startswith("v1,")
    assert signing.verify(secret, header, "msg_1", 1_000, body)


def test_verify_rejects_wrong_secret_or_tamper() -> None:
    secret = signing.generate_secret()
    other = signing.generate_secret()
    body = '{"a":1}'
    header = signing.signature_header([secret], "msg_1", 1_000, body)
    assert not signing.verify(other, header, "msg_1", 1_000, body)
    assert not signing.verify(secret, header, "msg_1", 1_000, '{"a":2}')  # tampered body
    assert not signing.verify(secret, header, "msg_1", 2_000, body)  # tampered timestamp


def test_rotation_signs_with_all_secrets() -> None:
    old = signing.generate_secret()
    new = signing.generate_secret()
    body = "{}"
    header = signing.signature_header([new, old], "msg_1", 1_000, body)
    # A receiver using either the old or the new secret verifies.
    assert signing.verify(old, header, "msg_1", 1_000, body)
    assert signing.verify(new, header, "msg_1", 1_000, body)
    assert header.count("v1,") == 2


def test_generated_secret_has_prefix() -> None:
    assert signing.generate_secret().startswith(signing.SECRET_PREFIX)
