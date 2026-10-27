"""A stand-in hub for ``evo-agents plugin test``: the plugin API of records, in memory, on a port of 127.0.0.1.

It answers what the hub answers a backend that asks for records (``docs/plugins.md``, Records): the plugin's token is
needed, the project is the one the checks run in, and every call needs ``records:use`` among the permissions the
manifest asks for. A call outside those bounds is refused as the hub refuses it and kept in ``violations``, so that
``plugin test`` names it. Standard library only.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

RECORDS = "records:use"
MAX_BODY = 64 * 1024


class FakeHub:
    """Serves ``/v1/plugin-api/projects/{project}/records/...`` for one plugin in one project."""

    def __init__(self, *, project: str, token: str, delegation: str, permissions: tuple[str, ...]):
        self.project = project
        self.token = token
        self.delegation = delegation
        self.permissions = frozenset(permissions)
        self.records: dict[tuple[str, str], dict] = {}
        self.calls: list[str] = []
        self.violations: list[str] = []
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/"

    def __enter__(self) -> FakeHub:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def _violation(self, text: str) -> None:
        with self._lock:
            if text not in self.violations:
                self.violations.append(text)

    def handle(self, method: str, url: str, headers, body: bytes) -> tuple[int, object]:
        """The status and JSON of the answer to one request."""
        parsed = urlparse(url)
        label = f"{method} {parsed.path}"
        with self._lock:
            self.calls.append(label)
        parts = [unquote(p) for p in parsed.path.strip("/").split("/")]
        if parts[:3] != ["v1", "plugin-api", "projects"] or len(parts) < 5 or parts[4] != "records":
            return 404, {"detail": "not found"}
        if headers.get("Authorization") != f"Bearer {self.token}":
            return 401, {"detail": "unknown plugin token"}
        if parts[3] != self.project:
            self._violation(f"{label}: a project other than the one it serves ({parts[3]})")
            return 404, {"detail": "not found"}
        if RECORDS not in self.permissions:
            self._violation(f"{label}: it keeps records without asking {RECORDS} in permissions")
            return 403, {"detail": f"the plugin was not granted {RECORDS}"}
        sent = headers.get("X-Evo-On-Behalf-Of")
        if sent is not None and sent != self.delegation:
            self._violation(f"{label}: X-Evo-On-Behalf-Of is not the delegation of the call it serves")
            return 403, {"detail": "unknown delegation"}
        rest = parts[5:]
        if not rest or len(rest) > 2:
            return 404, {"detail": "not found"}
        collection = rest[0]
        with self._lock:
            return self._records(method, collection, rest[1] if len(rest) == 2 else None, parsed.query, body)

    def _records(self, method: str, collection: str, key: str | None, query: str, body: bytes) -> tuple[int, object]:
        if key is None:
            if method != "GET":
                return 405, {"detail": "method not allowed"}
            items = [dict(r, key=k) for (c, k), r in sorted(self.records.items()) if c == collection]
            after = parse_qs(query).get("after", [""])[0]
            return 200, {"records": [r for r in items if r["key"] > after], "next_after": None}
        found = self.records.get((collection, key))
        if method == "GET":
            return (200, dict(found, key=key)) if found else (404, {"detail": "no such record"})
        if method == "DELETE":
            if found is None:
                return 404, {"detail": "no such record"}
            del self.records[(collection, key)]
            return 204, None
        if method == "PUT":
            try:
                data = json.loads(body or b"{}")
                if not isinstance(data, dict) or not isinstance(data.get("body"), dict) or len(body) > MAX_BODY:
                    raise ValueError
            except ValueError:
                return 422, {"detail": "body must be a JSON object of at most 64 KiB"}
            wanted = data.get("revision")
            if wanted is not None and wanted != (found["revision"] if found else 0):
                return 409, {"detail": "the record is not at that revision"}
            record = {
                "body": data["body"],
                "level": data.get("level") or "internal",
                "audience": data.get("audience") or "everyone",
                "revision": (found["revision"] if found else 0) + 1,
            }
            self.records[(collection, key)] = record
            return (200 if found else 201), dict(record, key=key)
        return 405, {"detail": "method not allowed"}

    def _handler(self):
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                size = min(int(self.headers.get("Content-Length") or 0), MAX_BODY + 1)
                status, payload = hub.handle(self.command, self.path, self.headers, self.rfile.read(size))
                data = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_PUT = do_DELETE = do_POST = _serve  # noqa: N815

            def log_message(self, *args) -> None:
                pass

        return Handler
