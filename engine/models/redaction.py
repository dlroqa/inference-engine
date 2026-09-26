"""Credential redaction for model sources and model error text.

One policy, shared by the model API (responses) and the model service (log
lines), so both remove the same credentials: URL userinfo (``user:pass@``) and
query values whose names look credential-bearing (``?token=``,
``X-Amz-Signature`` ...). Standard library only; this module must not depend on
the API, the service, the registry or logging setup.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, unquote_plus, urlencode, urlsplit, urlunsplit

_SENSITIVE_QUERY_HINTS = ("token", "key", "secret", "sig", "auth", "password", "credential")
_MASK = "***"
WITHHELD = "error details withheld (they could not be redacted safely)"

# A URL inside free-form text (an exception message): scheme://... up to
# whitespace, a quote, a parenthesis or a brace. Square brackets are allowed:
# they delimit IPv6 hosts (``https://[::1]:8443/...``).
_URL_IN_TEXT = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s'\"<>(){}]+")
# Userinfo right after "://" (``user:pass@``), up to the last "@" before the
# path, query, fragment or whitespace, as ``urlsplit`` splits it. Quotes and
# parentheses are valid in userinfo, so this does not stop where the URL
# matcher does.
_USERINFO = re.compile(r"(?<=://)[^/?#\s]*@")
# A query parameter anywhere in text, e.g. a request target without a scheme
# (``/m.gguf?token=abc``) as some HTTP errors report it. A value may contain
# quotes and parentheses, so a credential is not cut short where the URL
# matcher stops; trailing punctuation is split off separately.
_QUERY_PARAM = re.compile(r"([?&])([^=&#\s'\"<>]+)=([^&#\s\"<>]*)")
# Parameter values are searched for a nested URL or query (a redirect target
# such as ``?next=https://h/m?token=...``) this many levels deep. Past it, a
# value that could still carry one is masked rather than decoded again.
_MAX_NESTING = 1


def _is_sensitive(name: str) -> bool:
    """Whether a query parameter name (already URL-decoded) looks credential-bearing."""
    return any(h in name.lower() for h in _SENSITIVE_QUERY_HINTS)


def _is_sensitive_raw(raw_name: str) -> bool:
    """The same check for a name as written in text (e.g. ``%74oken``).

    The name is decoded exactly once, as ``parse_qsl`` decodes names in a full
    URL (percent-escapes and ``+``), so both paths classify the same name the
    same way. Invalid escapes are kept as written; decoding never raises.
    """
    return _is_sensitive(unquote_plus(raw_name, errors="replace"))


def _nested(decoded: str, depth: int) -> str:
    """Redacts a non-sensitive, already-decoded value that may embed a URL."""
    if depth >= _MAX_NESTING:
        return _MASK if any(c in decoded for c in "=@%") else decoded
    return _redact_text(decoded, depth + 1)


def _conservative_redact(url: str) -> str:
    """Redaction when a URL cannot be parsed: drop userinfo and the whole query."""
    url = _USERINFO.sub("", url.split("#", 1)[0])
    base, sep, _ = url.partition("?")
    return f"{base}?{_MASK}" if sep else base


def _redact_url(url: str, depth: int) -> str:
    try:
        parts = urlsplit(url)
        netloc = parts.hostname or ""
        if ":" in netloc:
            netloc = f"[{netloc}]"  # an IPv6 host keeps its brackets
        if parts.port is not None:
            netloc = f"{netloc}:{parts.port}"
        query = [
            (k, _MASK if _is_sensitive(k) else _nested(v, depth))
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
        ]
        return urlunsplit((parts.scheme, netloc, parts.path, urlencode(query, safe="*"), ""))
    except ValueError:
        return _conservative_redact(url)


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
    return _redact_url(source_ref, 0)


def _split_trailing(token: str) -> tuple[str, str]:
    """Separates sentence punctuation that follows a URL in prose."""
    end = len(token)
    while end > 0 and token[end - 1] in ".,;:!":
        end -= 1
    return token[:end], token[end:]


def _split_value_trailing(token: str) -> tuple[str, str]:
    """The same for a parameter value, which may also be closed by a quote or bracket."""
    end = len(token)
    while end > 0 and token[end - 1] in ".,;:!')]}":
        end -= 1
    return token[:end], token[end:]


def _redact_text(text: str, depth: int) -> str:
    def url(match: re.Match[str]) -> str:
        found, trail = _split_trailing(match.group(0))
        return _redact_url(found, depth) + trail

    def param(match: re.Match[str]) -> str:
        sep, name, raw = match.groups()
        value, trail = _split_value_trailing(raw)
        if value and _is_sensitive_raw(name):
            return f"{sep}{name}={_MASK}{trail}"
        decoded = unquote_plus(value, errors="replace")
        if value and _nested(decoded, depth) != decoded:
            return f"{sep}{name}={_MASK}{trail}"
        return match.group(0)

    text = _USERINFO.sub("", text)
    return _QUERY_PARAM.sub(param, _URL_IN_TEXT.sub(url, text))


def redact_urls_in_text(text: str | None) -> str | None:
    """Redact credentials from every URL and query parameter in free-form text.

    Used for the model ``error`` string and the download-failure log detail,
    which record raw exception messages that can quote the download URL, a
    redirect target, or a request target. Uses the same sensitive-name policy as
    :func:`redact_source_ref`: a name is classified after one URL decode, so
    ``?%74oken=`` is treated as ``?token=``. Userinfo and query parameters are
    found across the whole text, so protection does not depend on the URL
    matcher consuming a complete URL. Anything that is not credential-bearing
    is kept, so the error stays useful. If the text cannot be processed at all,
    it is withheld rather than returned raw.
    """
    if not text:
        return text
    try:
        return _redact_text(text, 0)
    except Exception:  # never return unredacted text
        return WITHHELD
