"""Standard Webhooks signing (https://www.standardwebhooks.com/).

Each delivery carries three headers:

- ``webhook-id``        — the unique message id (also the receiver's idempotency key)
- ``webhook-timestamp`` — unix seconds, for replay protection
- ``webhook-signature`` — one or more space-separated ``v1,<base64>`` signatures

The signed content is ``f"{id}.{timestamp}.{body}"`` and each signature is
``base64(HMAC_SHA256(secret_bytes, signed_content))``. Secrets are stored as
``whsec_<base64>``; the bytes used for HMAC are the base64-decoded portion after
the prefix. During rotation an endpoint has several active secrets, so a delivery
is signed with each and a receiver verifying with any one of them succeeds.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets as _secrets

SECRET_PREFIX = "whsec_"


def generate_secret(nbytes: int = 24) -> str:
    """Return a new Standard Webhooks signing secret (``whsec_<base64>``)."""
    raw = _secrets.token_bytes(nbytes)
    return SECRET_PREFIX + base64.b64encode(raw).decode("ascii")


def _secret_bytes(secret: str) -> bytes:
    body = secret[len(SECRET_PREFIX) :] if secret.startswith(SECRET_PREFIX) else secret
    try:
        return base64.b64decode(body)
    except (ValueError, base64.binascii.Error):  # type: ignore[attr-defined]
        # A non-base64 secret is used as raw bytes (still deterministic + secret).
        return body.encode("utf-8")


def sign(secret: str, msg_id: str, timestamp: int, body: str) -> str:
    """Return the bare base64 signature (no ``v1,`` prefix) for one secret."""
    signed_content = f"{msg_id}.{timestamp}.{body}".encode()
    digest = hmac.new(_secret_bytes(secret), signed_content, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def signature_header(secrets: list[str], msg_id: str, timestamp: int, body: str) -> str:
    """Build the ``webhook-signature`` header value from one or more secrets.

    Each secret contributes a ``v1,<base64>`` token; tokens are space-separated so
    a receiver that verifies against any single one accepts the delivery (this is
    what makes rotation seamless).
    """
    return " ".join(f"v1,{sign(s, msg_id, timestamp, body)}" for s in secrets)


def verify(secret: str, header: str, msg_id: str, timestamp: int, body: str) -> bool:
    """Receiver-side check: does ``header`` contain a valid ``v1`` signature for
    ``secret``? Constant-time comparison against every token in the header."""
    expected = sign(secret, msg_id, timestamp, body)
    ok = False
    for token in header.split():
        version, _, value = token.partition(",")
        if version == "v1" and hmac.compare_digest(value, expected):
            ok = True  # keep scanning to avoid early-exit timing signal
    return ok
