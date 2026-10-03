"""Recording proxy and replay server.

Both servers speak the OpenAI/Anthropic HTTP dialect (any JSON-over-HTTP API,
really):

* RecordServer sits between your app and the real API. Requests pass through
  untouched (with their auth headers) while every exchange is teed into a
  cassette. Streaming (SSE) responses are forwarded chunk-by-chunk as they
  arrive, then stored as discrete events.

* ReplayServer serves a cassette instead of the network. Requests are matched
  by method + path + canonical body hash (or in sequence, or loosely). It
  never touches the network, which makes test runs deterministic, offline,
  and free.

Paths are normalized (a single leading /v1 is stripped) so clients configured
with either `.../v1`-style base URLs or origin-style base URLs produce the
same match keys.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web
from aiohttp.web_runner import TCPSite

from .cassette import (
    Cassette,
    Exchange,
    compute_match_key,
    decode_body,
    encode_body,
    normalize_path,
    now_iso,
    parse_sse_events,
)
from .scrub import (
    redact_json,
    redact_text,
    scrub_request_headers,
    scrub_response_headers,
)

log = logging.getLogger("openvcr")

DEFAULT_PORT = 8817
SSE_CONTENT_TYPE = "text/event-stream"


class ProxyError(Exception):
    pass


# --------------------------------------------------------------------------- #
#  Record server
# --------------------------------------------------------------------------- #

class RecordServer:
    def __init__(self, upstream: str, cassette: Cassette,
                 scrub_body: bool = True,
                 extra_scrub_patterns: list | None = None):
        upstream = upstream.rstrip("/")
        if "://" not in upstream:
            raise ProxyError(
                f"--upstream must be an origin like https://api.openai.com (got {upstream!r})")
        self.upstream_origin = upstream
        self.cassette = cassette
        self.scrub_body = scrub_body
        self.extra_scrub_patterns = extra_scrub_patterns or []
        self.record_count = 0
        self._session: ClientSession | None = None
        self._write_lock = asyncio.Lock()

    def _redact(self, text: str) -> str:
        if not self.scrub_body:
            return text
        return redact_text(text, self.extra_scrub_patterns)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.read()
        forward_headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in ("host", "content-length", "accept-encoding",
                                 "connection", "transfer-encoding")
        }
        url = self.upstream_origin + request.rel_url.raw_path_qs

        started = asyncio.get_event_loop().time()
        try:
            assert self._session is not None
            upstream_resp = await self._session.request(
                request.method, url, data=body if body else None,
                headers=forward_headers, allow_redirects=False)
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller
            raise web.HTTPBadGateway(
                text=json.dumps({"error": {"type": "openvcr_upstream_error",
                                           "message": f"could not reach {self.upstream_origin}: {exc}"}}),
                content_type="application/json") from exc

        # Stream the response through while teeing a copy for the cassette.
        resp = web.StreamResponse(status=upstream_resp.status,
                                  reason=upstream_resp.reason)
        resp.content_type = upstream_resp.content_type or "application/octet-stream"
        await resp.prepare(request)

        chunks = bytearray()
        async for chunk in upstream_resp.content.iter_any():
            chunks += chunk
            await resp.write(chunk)
        await resp.write_eof()

        duration_ms = int((asyncio.get_event_loop().time() - started) * 1000)
        await self._save(request, body, upstream_resp, bytes(chunks), duration_ms)
        return resp

    async def _save(self, request, request_body, upstream_resp, response_body, duration_ms):
        is_stream = SSE_CONTENT_TYPE in (upstream_resp.headers.get("Content-Type") or "")
        response: dict = {"status": upstream_resp.status,
                          "headers": scrub_response_headers(dict(upstream_resp.headers))}
        if is_stream:
            response["stream"] = parse_sse_events(response_body)
            body_note = f"{len(response['stream'])} SSE events"
        else:
            encoded = encode_body(response_body)
            if "body" in encoded and self.scrub_body and encoded["body"] is not None:
                encoded["body"] = redact_json(encoded["body"], self.extra_scrub_patterns)
            elif "body_b64" in encoded and self.scrub_body:
                encoded["body_b64"] = self._redact(encoded["body_b64"])
            response.update(encoded)
            body_note = f"{len(response_body)} bytes"

        record = Exchange(
            recorded_at=now_iso(),
            duration_ms=duration_ms,
            key=compute_match_key(request.method, request.path, request_body),
            request={
                "method": request.method.upper(),
                "path": request.path,
                "query": dict(request.rel_url.query),
                "headers": scrub_request_headers(dict(request.headers)),
                **encode_body(request_body),
            },
            response=response,
        )
        async with self._write_lock:
            self.cassette.append(record)
            self.record_count += 1
        log.info("[record] %s %s -> %s (%d ms, %s)",
                 record.request["method"], record.request["path"],
                 upstream_resp.status, duration_ms, body_note)

    async def start(self, port: int, host: str = "127.0.0.1"):
        self._session = ClientSession(timeout=ClientTimeout(total=None, sock_connect=30))
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handle)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = TCPSite(runner, host, port)
        await site.start()
        actual_port = site._server.sockets[0].getsockname()[1]
        return runner, actual_port

    async def stop(self, runner):
        await runner.cleanup()
        if self._session:
            await self._session.close()


# --------------------------------------------------------------------------- #
#  Replay server
# --------------------------------------------------------------------------- #

class NoMatch(Exception):
    pass


class ReplayIndex:
    """Resolves incoming requests to recorded exchanges."""

    def __init__(self, cassette: Cassette, mode: str = "hash",
                 ignore_fields: frozenset[str] = frozenset()):
        self.mode = mode
        self.ignore_fields = ignore_fields
        self.records = cassette.load()
        # Keys are recomputed here with the *active* ignore_fields rather than
        # trusting keys stored at record time, so `--ignore-field` can loosen
        # matching for recordings that predate a schema change.
        self._hash_index: dict[str, Exchange] = {}
        self._path_index: dict[tuple[str, str], list[Exchange]] = {}
        for rec in self.records:
            body = decode_body(rec.request.get("body"), rec.request.get("body_b64"))
            key = compute_match_key(rec.request.get("method", "GET"),
                                    rec.request.get("path", "/"), body, ignore_fields)
            self._hash_index.setdefault(key, rec)
            pk = (rec.request.get("method", ""), normalize_path(rec.request.get("path", "/")))
            self._path_index.setdefault(pk, []).append(rec)
        self._sequence: dict[tuple[str, str], int] = {}

    def resolve(self, method: str, path: str, body: bytes) -> Exchange:
        pkey = (method.upper(), normalize_path(path))
        if self.mode == "sequence":
            candidates = self._path_index.get(pkey, [])
            i = self._sequence.get(pkey, 0)
            self._sequence[pkey] = i + 1
            if i < len(candidates):
                return candidates[i]
            raise NoMatch(f"no {len(candidates)} recorded exchange(s) left for "
                          f"{method} {path} in sequence mode")

        key = compute_match_key(method, path, body, self.ignore_fields)
        if self.mode == "hash":
            hit = self._hash_index.get(key)
            if hit:
                return hit
        elif self.mode == "any":
            candidates = self._path_index.get(pkey, [])
            if candidates:
                return candidates[0]

        known = sorted({(m, p) for (m, p) in self._path_index})[:8]
        known_lines = "\n".join(f"  {m} {p}" for m, p in known) or "  (cassette is empty)"
        raise NoMatch(
            f"No cassette entry matched {method} {path} (key {key[:24]}…).\n"
            f"Cassette contains {len(self.records)} exchange(s):\n{known_lines}\n"
            f"Re-record with: openvcr record --upstream <api-origin> --out <cassette>")


class ReplayServer:
    def __init__(self, cassette: Cassette, mode: str = "hash",
                 latency: str = "fast",
                 ignore_fields: frozenset[str] = frozenset()):
        self.index = ReplayIndex(cassette, mode, ignore_fields)
        self.latency = latency
        self.hits = 0

    async def handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.read()
        try:
            record = self.index.resolve(request.method, request.path, body)
        except NoMatch as exc:
            return web.json_response(
                {"error": {"type": "openvcr_no_match", "message": str(exc)}},
                status=409)

        response = record.response
        if response.get("stream") is not None:
            return await self._replay_stream(request, record)

        payload = decode_body(response.get("body"), response.get("body_b64"))
        headers = dict(response.get("headers") or {})
        content_type = headers.get("Content-Type") or headers.get("content-type") \
            or "application/json"
        await self._pace(record)
        self.hits += 1
        log.info("[replay] %s %s -> %s (recorded %s)",
                 request.method, request.path, response.get("status"),
                 record.recorded_at or "unknown time")
        return web.Response(body=payload, status=response.get("status", 200),
                            content_type=content_type.split(";")[0].strip() or "application/json")

    async def _replay_stream(self, request, record) -> web.StreamResponse:
        resp = web.StreamResponse(status=record.response.get("status", 200))
        resp.content_type = SSE_CONTENT_TYPE
        await resp.prepare(request)
        events = record.response["stream"]
        per_event = 0.0
        if self.latency == "real" and record.duration_ms > 0:
            per_event = record.duration_ms / max(len(events), 1) / 1000.0
        for event in events:
            if per_event:
                await asyncio.sleep(per_event)
            await resp.write((event + "\n\n").encode("utf-8"))
            self.hits += 1
        await resp.write_eof()
        log.info("[replay] %s %s -> %s SSE stream (%d events, recorded %s)",
                 request.method, request.path, record.response.get("status"),
                 len(events), record.recorded_at or "unknown time")
        return resp

    async def _pace(self, record):
        if self.latency == "real" and record.duration_ms > 0:
            await asyncio.sleep(record.duration_ms / 1000.0)

    async def start(self, port: int, host: str = "127.0.0.1"):
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handle)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = TCPSite(runner, host, port)
        await site.start()
        actual_port = site._server.sockets[0].getsockname()[1]
        return runner, actual_port

    @staticmethod
    async def stop(runner):
        await runner.cleanup()


# --------------------------------------------------------------------------- #
#  Threaded runner (used by the pytest plugin; the CLI runs on the main loop)
# --------------------------------------------------------------------------- #

class ServerHandle:
    """A running record/replay server on its own thread + event loop."""

    def __init__(self, server: RecordServer | ReplayServer, port: int = 0,
                 host: str = "127.0.0.1"):
        self._server = server
        self._host = host
        self._requested_port = port
        self.port: int | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop_loop: asyncio.AbstractEventLoop | None = None
        self._runner = None
        self.error: str | None = None

    def __enter__(self) -> "ServerHandle":
        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._stop_loop = loop
            try:
                runner, port = loop.run_until_complete(
                    self._server.start(self._requested_port, self._host))
                self._runner = runner
                self.port = port
            except Exception as exc:  # noqa: BLE001 - re-raised in main thread
                self.error = str(exc)
                self._ready.set()
                return
            self._ready.set()
            loop.run_forever()
            loop.run_until_complete(self._server.stop(runner) if isinstance(
                self._server, RecordServer) else ReplayServer.stop(runner))
            loop.close()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=15)
        if self.error:
            raise ProxyError(f"could not start server: {self.error}")
        return self

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self.port}"

    def __exit__(self, *exc) -> None:
        if self._stop_loop:
            self._stop_loop.call_soon_threadsafe(self._stop_loop.stop)
        if self._thread:
            self._thread.join(timeout=10)
        self._thread = None


def read_cassette(path: str | Path) -> Cassette:
    return Cassette(path)
