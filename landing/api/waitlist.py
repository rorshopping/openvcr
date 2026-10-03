"""OpenVCR waitlist API — dependency-light ASGI app for the Vercel Python runtime.

POST /api/waitlist  {email, role?, building?, website?}  -> {"ok": true, "position": n}
GET  /api/waitlist                                       -> {"ok": true, "count": n}

Storage: a **private Vercel Blob** object (`waitlist/signups.jsonl`, one JSON
signup per line), accessed through Vercel's presigned-blob REST API with
nothing but the standard library — see BlobStore below for the protocol.
Without BLOB_READ_WRITE_TOKEN the API falls back to a local JSONL file, which
is fine for development but is NOT durable on serverless (see DEPLOY.md).

`dispatch()` is deliberately framework-free so landing/devserver.py can drive
the exact same code path locally.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

MAX_BODY_BYTES = 4096
RATE_LIMIT = 10            # requests ...
RATE_WINDOW_S = 600        # ... per 10 minutes per IP
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$")
VALID_ROLES = {"builder", "platform", "enterprise", "other"}
FALLBACK_PATH = Path("/tmp/openvcr-waitlist.jsonl")
BLOB_PATHNAME = "waitlist/signups.jsonl"

_lock = threading.Lock()
_rate: dict[str, list[float]] = {}
_signed_token_cache: dict[tuple, tuple[str, str, float]] = {}  # (ops) -> (cst, delegation, exp)


# --------------------------------------------------------------------------- #
#  Private Vercel Blob store (stdlib-only presign protocol)
# --------------------------------------------------------------------------- #

class BlobStore:
    """Append-style JSONL in a private Blob container.

    The protocol (reverse-engineered from @vercel/blob and verified live):
      1. POST /api/blob/signed-token {pathname, operations} -> {clientSigningToken,
         delegationToken} scoped to exactly this pathname.
      2. Signature = base64url(HMAC-SHA256(clientSigningToken,
         "\n".join(sorted(["operation=<op>", "pathname=<path>", *entries]))))
      3. PUT https://vercel.com/api/blob/?pathname=...&<entries>&delegation&signature
         GET https://<store>.private.blob.vercel-storage.com/<path>?delegation&signature
    """

    def __init__(self, token: str, store_id: str):
        self.token = token
        self.store_id = store_id
        self.blob_host = f"{store_id.removeprefix('store_')}.private.blob.vercel-storage.com"
        # Blob storage is eventually consistent: a read right after a write can
        # 404 briefly, so reads retry with a fresh signature.
        self.read_retries = 3

    # -- protocol -----------------------------------------------------------
    def _signed_token(self, operations: list[str]) -> tuple[str, str]:
        key = tuple(operations)
        now = time.time()
        cached = _signed_token_cache.get(key)
        if cached and cached[2] > now:
            return cached[0], cached[1]
        body = json.dumps({"pathname": BLOB_PATHNAME, "operations": operations}).encode()
        req = urllib.request.Request(f"https://vercel.com/api/blob/signed-token",
                                     method="POST", data=body)
        for k, v in self._api_headers().items():
            req.add_header(k, v)
        req.add_header("content-type", "application/json")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        cst, delegation = data["clientSigningToken"], data["delegationToken"]
        _signed_token_cache[key] = (cst, delegation, now + 30)
        return cst, delegation

    def _api_headers(self) -> dict:
        return {
            "authorization": f"Bearer {self.token}",
            "x-api-version": "12",
            "x-api-blob-request-id": f"{self.store_id}:{int(time.time() * 1000)}:"
                                     f"{os.urandom(4).hex()}",
            "x-vercel-blob-store-id": self.store_id,
            "x-api-blob-request-attempt": "0",
        }

    def _sign(self, client_signing_token: str, operation: str,
              entries: list[tuple[str, str]] = ()) -> str:
        lines = [f"operation={operation}", f"pathname={BLOB_PATHNAME}"]
        lines += [f"{k}={v}" for k, v in entries]
        canonical = "\n".join(sorted(lines, key=lambda s: s.encode()))
        digest = hmac.new(client_signing_token.encode(), canonical.encode(),
                          hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")

    # -- operations ----------------------------------------------------------
    def read(self) -> bytes | None:
        last_error: Exception | None = None
        for _ in range(self.read_retries):
            cst, delegation = self._signed_token(["get"])
            sig = self._sign(cst, "get")
            url = (f"https://{self.blob_host}/{BLOB_PATHNAME}"
                   f"?vercel-blob-delegation={urllib.parse.quote(delegation, safe='')}"
                   f"&vercel-blob-signature={sig}")
            try:
                with urllib.request.urlopen(url, timeout=15) as resp:
                    return resp.read()
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None  # genuinely empty store
                last_error = e
            except Exception as e:  # noqa: BLE001 - retried, then raised
                last_error = e
            time.sleep(0.3)
        raise last_error  # type: ignore[misc]

    def write(self, data: bytes) -> None:
        entries = [("vercel-blob-add-random-suffix", "false"),
                   ("vercel-blob-allow-overwrite", "true")]
        cst, delegation = self._signed_token(["put"])
        sig = self._sign(cst, "put", entries)
        params = "&".join(
            [f"pathname={urllib.parse.quote(BLOB_PATHNAME, safe='')}"]
            + [f"{k}={v}" for k, v in entries]
            + [f"vercel-blob-delegation={urllib.parse.quote(delegation, safe='')}",
               f"vercel-blob-signature={sig}"])
        req = urllib.request.Request(f"https://vercel.com/api/blob/?{params}",
                                     method="PUT", data=data)
        for k, v in self._api_headers().items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()

    def _parse(self, raw: bytes | None) -> list[dict]:
        if not raw:
            return []
        out = []
        for line in raw.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def insert(self, email: str, role: str, building: str) -> int:
        with _lock:
            rows = self._parse(self.read())
            if any(r.get("email") == email.lower() for r in rows):
                return len(rows)
            rows.append({"email": email.lower(), "role": role,
                         "building": building, "ts": int(time.time())})
            self.write(("\n".join(json.dumps(r) for r in rows) + "\n").encode())
            return len(rows)

    def count(self) -> int:
        with _lock:
            return len(self._parse(self.read()))


class FileStore:
    """Development fallback: append-only JSONL on the local filesystem."""

    def __init__(self, path: Path):
        self.path = path

    def _rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def insert(self, email: str, role: str, building: str) -> int:
        with _lock:
            rows = self._rows()
            if any(r.get("email") == email.lower() for r in rows):
                return len(rows)
            rows.append({"email": email.lower(), "role": role,
                         "building": building, "ts": int(time.time())})
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                                 encoding="utf-8")
            return len(rows)

    def count(self) -> int:
        with _lock:
            return len(self._rows())


_store_singleton = None
_store_kind = "unset"


def _store():
    global _store_singleton, _store_kind
    if _store_singleton is not None:
        return _store_singleton
    token = os.environ.get("BLOB_READ_WRITE_TOKEN", "")
    if token:
        # Vercel's token encodes the store id as `vercel_blob_rw_<id>_<sig>`;
        # the store id is also mirrored in BLOB_STORE_URL-style vars when
        # present, so prefer explicit env, else derive from the token.
        store_id = os.environ.get("BLOB_STORE_ID") or _store_id_from_token(token)
        _store_singleton = BlobStore(token, store_id)
        _store_kind = "blob"
    else:
        _store_singleton = FileStore(FALLBACK_PATH)
        _store_kind = "file"
    return _store_singleton


def _store_id_from_token(token: str) -> str:
    """Vercel Blob RW tokens look like vercel_blob_rw_<store_id>_<signature>."""
    body = token.removeprefix("vercel_blob_rw_")
    return body.rsplit("_", 1)[0]


# --------------------------------------------------------------------------- #
#  Rate limiting (best-effort, per instance)
# --------------------------------------------------------------------------- #

def _rate_ok(ip: str) -> bool:
    now = time.time()
    with _lock:
        hits = [t for t in _rate.get(ip, []) if now - t < RATE_WINDOW_S]
        if len(hits) >= RATE_LIMIT:
            _rate[ip] = hits
            return False
        hits.append(now)
        _rate[ip] = hits
        if len(_rate) > 5000:  # keep the map bounded
            _rate.clear()
        return True


# --------------------------------------------------------------------------- #
#  Core dispatch (shared by ASGI app and the local dev server)
# --------------------------------------------------------------------------- #

def dispatch(method: str, body_bytes: bytes, ip: str) -> tuple[int, dict]:
    if method == "GET":
        try:
            return 200, {"ok": True, "count": _store().count()}
        except Exception as exc:  # noqa: BLE001
            return 200, {"ok": True, "count": 0, "warning": f"count unavailable: {exc}"}

    if method != "POST":
        return 405, {"ok": False, "error": {"message": "method not allowed"}}

    if not _rate_ok(ip):
        return 429, {"ok": False, "error": {"message": "too many requests — slow down a little"}}

    if len(body_bytes) > MAX_BODY_BYTES:
        return 413, {"ok": False, "error": {"message": "payload too large"}}

    try:
        data = json.loads(body_bytes or b"{}")
    except ValueError:
        return 400, {"ok": False, "error": {"message": "invalid JSON"}}

    if data.get("website"):  # honeypot — pretend success, store nothing
        return 200, {"ok": True, "position": None}

    email = str(data.get("email") or "").strip()
    if not EMAIL_RE.match(email) or len(email) > 254:
        return 400, {"ok": False, "error": {"message": "please provide a valid email address"}}

    role = str(data.get("role") or "builder")
    if role not in VALID_ROLES:
        role = "other"
    building = str(data.get("building") or "")[:500]

    try:
        total = _store().insert(email, role, building)
    except Exception as exc:  # noqa: BLE001
        return 500, {"ok": False, "error": {"message": f"storage error: {exc}"}}
    return 200, {"ok": True, "position": total, "store": _store_kind}


# --------------------------------------------------------------------------- #
#  Minimal ASGI app
# --------------------------------------------------------------------------- #

async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    method = scope["method"].upper()
    headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
    ip = (scope.get("client") or ("unknown", 0))[0] or headers.get("x-forwarded-for", "unknown").split(",")[0].strip()

    body = b""
    while True:
        message = await receive()
        if message["type"] == "http.request":
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        elif message["type"] == "http.disconnect":
            return

    status, payload = dispatch(method, body, ip)
    payload_bytes = json.dumps(payload).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": payload_bytes})
