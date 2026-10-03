"""Secret scrubbing for cassettes.

Recordings contain prompts and completions by design — but they must never
contain credentials. Headers are allowlisted at record time; this module adds
best-effort redaction of token-shaped strings inside bodies so cassettes are
safe to commit and share.
"""

from __future__ import annotations

import re

# header names dropped from recordings regardless of allowlists
SCRUB_HEADERS = {
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "api-key",
    "apikey",
    "cookie",
    "set-cookie",
    "openai-organization",
    "openai-project",
    "x-goog-api-key",
    "azure-api-key",
}

# token-shaped strings redacted inside bodies (best effort)
SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("openai", re.compile(r"sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}")),
    ("anthropic", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("github", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("github-finegrained", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("slack", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("google-api", re.compile(r"AIza[0-9A-Za-z_-]{30,}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{8,}")),
    ("bearer", re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]{20,}={0,2}")),
]

REDACTED = "[[REDACTED]]"


def redact_text(text: str, extra_patterns: list[re.Pattern] | None = None) -> str:
    for _name, pattern in SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    for pattern in extra_patterns or []:
        text = pattern.sub(REDACTED, text)
    return text


def redact_json(obj, extra_patterns: list[re.Pattern] | None = None):
    """Recursively redact strings inside parsed JSON structures."""
    if isinstance(obj, str):
        return redact_text(obj, extra_patterns)
    if isinstance(obj, list):
        return [redact_json(v, extra_patterns) for v in obj]
    if isinstance(obj, dict):
        return {k: redact_json(v, extra_patterns) for k, v in obj.items()}
    return obj


def scrub_request_headers(headers) -> dict[str, str]:
    """Allowlist-based: keep only headers that cannot carry credentials."""
    out = {}
    for name, value in headers.items():
        if name.lower() in REQUEST_KEEP:
            out[name] = value
    return out


REQUEST_KEEP = frozenset(("content-type", "accept", "user-agent"))


def scrub_response_headers(headers) -> dict[str, str]:
    out = {}
    for name, value in headers.items():
        if name.lower() in RESPONSE_KEEP:
            out[name] = value
    return out


RESPONSE_KEEP = frozenset(("content-type",))
