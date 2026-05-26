"""Privacy helpers for private dump analysis.

These helpers run before model calls and before rendering operator-facing
artifacts. They intentionally favor over-redaction for credential-shaped text.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s().-]{7,}\d)(?!\d)")
_DISCORD_INVITE_RE = re.compile(r"https?://(?:www\.)?(?:discord\.gg|discord\.com/invite)/\S+", re.I)
_SOURCE_SPECIFIC_COMMUNITY_RE = re.compile(r"\b" + "latent" + r"[\s.-]*" + "space" + r"\b", re.I)
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I)
_SECRET_TOKEN_RE = re.compile(
    r"\b(?:sk|pk|ghp|gho|xox[baprs]?|discord)[-_][A-Za-z0-9._~+/=-]{6,}\b",
    re.I,
)
_URL_RE = re.compile(r"https?://[^\s<>)\]]+")
_SENSITIVE_QUERY_KEYS = {
    "access_token",
    "api_key",
    "auth",
    "bearer",
    "code",
    "key",
    "password",
    "secret",
    "sig",
    "signature",
    "token",
}
_TRACKING_PREFIXES = ("utm_",)
_TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref_src"}


def redact_sensitive_text(text: str) -> str:
    """Return text with high-risk PII and credential-shaped content redacted."""

    redacted = _EMAIL_RE.sub("[redacted-email]", text)
    redacted = _PHONE_RE.sub("[redacted-phone]", redacted)
    redacted = _DISCORD_INVITE_RE.sub("[redacted-discord-invite]", redacted)
    redacted = _SOURCE_SPECIFIC_COMMUNITY_RE.sub("[redacted-community]", redacted)
    redacted = _BEARER_RE.sub("Bearer [redacted-token]", redacted)
    redacted = _SECRET_TOKEN_RE.sub("[redacted-secret]", redacted)
    return _URL_RE.sub(lambda m: _redact_url(m.group(0)), redacted)


def _redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return "[redacted-url]"

    if not parts.query:
        return url

    safe_query: list[tuple[str, str]] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        key_l = key.lower()
        if key_l.startswith(_TRACKING_PREFIXES) or key_l in _TRACKING_KEYS:
            continue
        if key_l in _SENSITIVE_QUERY_KEYS:
            safe_query.append((key, "[redacted]"))
            continue
        safe_query.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(safe_query), parts.fragment))
