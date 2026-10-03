"""Local dev server for the landing page: static files + the real waitlist API.

Runs the exact `dispatch()` code path that the Vercel function runs, without
needing `vercel dev` or a database (uses the /tmp fallback store).

    python3 scripts/serve_landing.py   # http://127.0.0.1:8080
"""

import importlib.util
import json
import mimetypes
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LANDING = Path(__file__).resolve().parent.parent / "landing"
API = LANDING / "api"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080

spec = importlib.util.spec_from_file_location("waitlist_api", API / "waitlist.py")
waitlist_api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(waitlist_api)

MIME_OVERRIDES = {".js": "text/javascript", ".css": "text/css", ".html": "text/html; charset=utf-8"}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, ctype: str):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.startswith("/api/"):
            status, payload = waitlist_api.dispatch("GET", b"", self.client_address[0])
            return self._send(status, json.dumps(payload).encode(), "application/json")
        if path == "/":
            path = "/index.html"
        file = (LANDING / path.lstrip("/")).resolve()
        if not str(file).startswith(str(LANDING)) or not file.is_file():
            return self._send(404, b"not found", "text/plain")
        ctype = MIME_OVERRIDES.get(file.suffix, mimetypes.guess_type(file.name)[0] or "application/octet-stream")
        self._send(200, file.read_bytes(), ctype)

    def do_POST(self):
        if not self.path.startswith("/api/"):
            return self._send(404, b"not found", "text/plain")
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(min(length, 65536))
        status, payload = waitlist_api.dispatch("POST", body, self.client_address[0])
        self._send(status, json.dumps(payload).encode(), "application/json")

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


if __name__ == "__main__":
    print(f"OpenVCR landing dev server → http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
