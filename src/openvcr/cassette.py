"""Cassette format: the JSONL file of recorded request/response exchanges.

A cassette is one JSON object per line. Each record looks like:

    {"v": 1,
     "recorded_at": "2026-10-03T12:00:00Z",
     "duration_ms": 412,
     "key": "sha256:...",
     "request":  {"method": "POST", "path": "/v1/chat/completions",
                  "headers": {...scrubbed...}, "body": {...} | null,
                  "body_b64": "..." | null},
     "response": {"status": 200, "headers": {...},
                  "body": {...} | null, "body_b64": "..." | null,
                  "stream": ["data: {...}", ...] | null}}

Records are append-only; players tolerate a torn final line (crash during
recording) by skipping it.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

CASSETTE_VERSION = 1

# Request headers that are always kept in recordings. Everything else
# (authorization, cookies, org ids...) is dropped at record time.
REQUEST_HEADER_ALLOWLIST = ("content-type", "accept", "user-agent")
# Response headers kept in recordings; replay reconstructs the rest.
RESPONSE_HEADER_ALLOWLIST = ("content-type",)

# Path normalization: OpenAI-style clients put /v1 in the base URL while
# Anthropic-style clients put it in the request path. Strip one leading
# /v1 so both land on the same key.
VERSION_PREFIX = "/v1"


def normalize_path(path: str) -> str:
    path = "/" + path.lstrip("/")
    if path.startswith(VERSION_PREFIX + "/") or path == VERSION_PREFIX:
        path = path[len(VERSION_PREFIX):]
    return path or "/"


def canonical_json(obj, ignore_fields: frozenset[str] = frozenset()) -> str:
    """Stable JSON rendering: sorted keys, no whitespace, optional field drops."""

    def walk(node):
        if isinstance(node, dict):
            return {
                k: walk(v)
                for k, v in sorted(node.items())
                if k not in ignore_fields
            }
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return json.dumps(walk(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_match_key(method: str, path: str, body: bytes,
                      ignore_fields: frozenset[str] = frozenset()) -> str:
    """Stable identity of a request: method + normalized path + canonical body."""
    norm_path = normalize_path(path)
    try:
        parsed = json.loads(body) if body else None
        body_repr = canonical_json(parsed, ignore_fields) if parsed is not None else (
            body.hex() if body else "")
        matched = "json"
    except (ValueError, UnicodeDecodeError):
        body_repr = hashlib.sha256(body).hexdigest() if body else ""
        matched = "raw"
    digest = hashlib.sha256(f"{method.upper()}\n{norm_path}\n{matched}\n{body_repr}".encode()).hexdigest()
    return "sha256:" + digest[:32]


def encode_body(data: bytes) -> dict:
    """Encode a raw body for storage; JSON is kept readable, anything else base64."""
    try:
        text = data.decode("utf-8")
        return {"body": json.loads(text) if text.strip() else None}
    except (ValueError, UnicodeDecodeError):
        return {"body": None, "body_b64": base64.b64encode(data).decode("ascii")}


def decode_body(record_body: dict | None, record_b64: str | None) -> bytes:
    if record_b64:
        return base64.b64decode(record_b64)
    if record_body is None:
        return b""
    return json.dumps(record_body, separators=(",", ":")).encode("utf-8")


def parse_sse_events(buffer: bytes) -> list[str]:
    """Split a raw SSE byte stream into its textual events (blank-line separated)."""
    text = buffer.decode("utf-8", errors="replace")
    events = [e.strip("\r\n") for e in text.split("\n\n")]
    return [e for e in events if e.strip()]


@dataclass
class Exchange:
    """One recorded request/response exchange."""
    request: dict
    response: dict
    recorded_at: str = ""
    duration_ms: int = 0
    key: str = ""
    meta: dict = field(default_factory=dict)

    def to_json(self) -> str:
        obj = {
            "v": CASSETTE_VERSION,
            "recorded_at": self.recorded_at,
            "duration_ms": self.duration_ms,
            "key": self.key,
            "request": self.request,
            "response": self.response,
        }
        if self.meta:
            obj["meta"] = self.meta
        return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> "Exchange":
        obj = json.loads(line)
        return cls(
            request=obj["request"],
            response=obj["response"],
            recorded_at=obj.get("recorded_at", ""),
            duration_ms=obj.get("duration_ms", 0),
            key=obj.get("key", ""),
            meta=obj.get("meta", {}),
        )


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Cassette:
    """Append-only writer / random-access reader of cassette files."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._records: list[Exchange] | None = None

    # -- writing ------------------------------------------------------------
    def append(self, exchange: Exchange) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(exchange.to_json() + "\n")
        self._records = None  # invalidate cache

    # -- reading ------------------------------------------------------------
    def load(self) -> list[Exchange]:
        if self._records is None:
            records = []
            if self.path.exists():
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(Exchange.from_json(line))
                    except (json.JSONDecodeError, KeyError):
                        break  # torn final line from an interrupted recording
            self._records = records
        return self._records

    def __len__(self) -> int:
        return len(self.load())

    def index_by_key(self, ignore_fields: frozenset[str] = frozenset()) -> dict[str, Exchange]:
        """Map of match key -> first matching exchange (recompute covers legacy cassettes)."""
        index: dict[str, Exchange] = {}
        for rec in self.load():
            key = rec.key or compute_match_key(
                rec.request.get("method", "GET"),
                rec.request.get("path", "/"),
                decode_body(rec.request.get("body"), rec.request.get("body_b64")),
                ignore_fields,
            )
            index.setdefault(key, rec)
        return index
