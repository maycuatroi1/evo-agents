"""A fake GitHub for the hub tests: github.com and api.github.com in one local HTTP server, so the real GitHub is
never called.

It answers what the hub and its CLI use: the device flow (POST /login/device/code, then polling POST
/login/oauth/access_token), the web flow (GET /login/oauth/authorize redirecting back with a code, and the code
exchange checked against the client secret, the redirect URI and the PKCE S256 challenge), GET /user and POST
/applications/{client_id}/token. A test scripts what the next device flow answers, chooses who is signed in to
the fake github.com in the browser, makes every answer a 503 or slow, and reads back every request and every code
and token the fake handed out.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


@dataclass(frozen=True)
class Account:
    login: str
    id: int


@dataclass
class Recorded:
    method: str
    path: str
    query: dict
    headers: dict
    body: str


@dataclass
class _Device:
    account: Account
    steps: list[str]  # answers before the token: authorization_pending, slow_down, or a final error
    interval: int


@dataclass
class _Code:
    account: Account
    client_id: str
    redirect_uri: str
    challenge: str


@dataclass
class FakeGitHub:
    client_id: str = "Iv1.fake0client0id"
    client_secret: str = field(default_factory=lambda: "fake-client-secret-" + secrets.token_hex(16))
    device_account: Account | None = None
    device_steps: list[str] = field(default_factory=list)
    interval: int = 0  # seconds the device flow asks the CLI to wait between polls
    browser_account: Account | None = None  # who is signed in to github.com in the browser of the web flow
    status: int | None = None  # answer every request with this status instead
    delay: float = 0.0  # seconds to wait before answering
    requests: list[Recorded] = field(default_factory=list)
    tokens: dict = field(default_factory=dict)  # token -> (Account, client id it was issued to, or None)
    codes: dict = field(default_factory=dict)  # web flow code -> _Code
    device_codes: dict = field(default_factory=dict)  # device code -> _Device
    issued_codes: list[str] = field(default_factory=list)

    def __post_init__(self):
        self._lock = threading.Lock()
        self._server = _QuietServer(("127.0.0.1", 0), _handler(self))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> FakeGitHub:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def stop(self) -> None:
        if self._thread.is_alive():
            self._server.shutdown()
        self._server.server_close()

    def issue_token(self, account: Account, *, app: bool = True) -> str:
        """A GitHub token of ``account``; ``app`` says whether this app issued it, as /applications checks."""
        token = "gho_" + secrets.token_hex(18)
        with self._lock:
            self.tokens[token] = (account, self.client_id if app else None)
        return token

    def secrets(self) -> set[str]:
        """Every value the fake handed out or holds that must never reach a hub log."""
        with self._lock:
            return {self.client_secret, *self.tokens, *self.issued_codes, *self.device_codes}

    def calls(self, path: str) -> list[Recorded]:
        return [r for r in self.requests if r.path == path]

    # Endpoints: (status, JSON body) or (status, None, headers) for a redirect.

    def _device_code(self, form: dict):
        if form.get("client_id") != self.client_id:
            return 404, {"error": "Not Found"}
        if self.device_account is None:
            return 400, {"error": "device_flow_disabled"}
        code = "dc_" + secrets.token_hex(16)
        with self._lock:
            self.device_codes[code] = _Device(self.device_account, list(self.device_steps), self.interval)
        return 200, {
            "device_code": code,
            "user_code": "WDJB-MJHT",
            "verification_uri": f"{self.url}/login/device",
            "expires_in": 900,
            "interval": self.interval,
        }

    def _access_token(self, form: dict):
        if form.get("grant_type") == DEVICE_GRANT:
            device = self.device_codes.get(form.get("device_code", ""))
            if form.get("client_id") != self.client_id or device is None:
                return 200, {"error": "incorrect_device_code"}
            if device.steps:
                step = device.steps[0] if device.steps[0] in ("expired_token", "access_denied") else device.steps.pop(0)
                if step == "slow_down":
                    device.interval += 5
                    return 200, {"error": "slow_down", "interval": device.interval}
                return 200, {"error": step}
            return 200, {"access_token": self.issue_token(device.account), "token_type": "bearer", "scope": "read:user"}
        if form.get("client_id") != self.client_id or form.get("client_secret") != self.client_secret:
            return 200, {"error": "incorrect_client_credentials"}
        with self._lock:
            code = self.codes.pop(form.get("code", ""), None)
        if code is None:
            return 200, {"error": "bad_verification_code"}
        if form.get("redirect_uri") != code.redirect_uri:
            return 200, {"error": "redirect_uri_mismatch"}
        verifier = form.get("code_verifier", "")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        if challenge != code.challenge:
            return 200, {"error": "bad_verification_code"}
        return 200, {"access_token": self.issue_token(code.account), "token_type": "bearer", "scope": "read:user"}

    def _authorize(self, query: dict):
        if query.get("client_id") != self.client_id or query.get("code_challenge_method") != "S256":
            return 400, {"error": "bad request"}
        if self.browser_account is None:
            return 403, {"error": "nobody is signed in to the fake github.com"}
        code = secrets.token_hex(10)
        with self._lock:
            redirect_uri, challenge = query["redirect_uri"], query["code_challenge"]
            self.codes[code] = _Code(self.browser_account, self.client_id, redirect_uri, challenge)
            self.issued_codes.append(code)
        location = query["redirect_uri"] + "?" + urlencode({"code": code, "state": query.get("state", "")})
        return 302, None, {"Location": location}

    def _user(self, headers: dict):
        scheme, _, token = headers.get("authorization", "").partition(" ")
        found = self.tokens.get(token) if scheme.lower() in ("bearer", "token") else None
        if found is None:
            return 401, {"message": "Bad credentials"}
        account = found[0]
        return 200, {"login": account.login, "id": account.id, "type": "User"}

    def _check_token(self, client_id: str, headers: dict, body: str):
        scheme, _, encoded = headers.get("authorization", "").partition(" ")
        given = base64.b64decode(encoded).decode() if scheme.lower() == "basic" else ""
        if client_id != self.client_id or given != f"{self.client_id}:{self.client_secret}":
            return 401, {"message": "Bad credentials"}
        token = json.loads(body or "{}").get("access_token")
        found = self.tokens.get(token)
        if found is None or found[1] != client_id:
            return 404, {"message": "Not Found"}
        account = found[0]
        return 200, {"app": {"client_id": client_id}, "user": {"login": account.login, "id": account.id}}

    def answer(self, method: str, path: str, query: dict, headers: dict, body: str):
        self.requests.append(Recorded(method, path, query, headers, body))
        if self.delay:
            time.sleep(self.delay)
        if self.status is not None:
            return self.status, {"message": "unavailable"}
        form = {k: v[0] for k, v in parse_qs(body).items()} if method == "POST" else {}
        if method == "POST" and path == "/login/device/code":
            return self._device_code(form)
        if method == "POST" and path == "/login/oauth/access_token":
            return self._access_token(form)
        if method == "GET" and path == "/login/oauth/authorize":
            return self._authorize(query)
        if method == "GET" and path == "/user":
            return self._user(headers)
        if method == "POST" and path.startswith("/applications/") and path.endswith("/token"):
            return self._check_token(path.split("/")[2], headers, body)
        if path == "/moved":
            return 302, None, {"Location": f"{self.url}/user"}
        return 404, {"message": "Not Found"}


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):  # a client that timed out and hung up is expected here
        pass


def _handler(fake: FakeGitHub):
    class Handler(BaseHTTPRequestHandler):
        def _serve(self, method: str) -> None:
            parts = urlsplit(self.path)
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length).decode() if length else ""
            query = {k: v[0] for k, v in parse_qs(parts.query).items()}
            headers = {k.lower(): v for k, v in self.headers.items()}
            status, payload, *extra = fake.answer(method, parts.path, query, headers, body)
            data = b"" if payload is None else json.dumps(payload).encode()
            self.send_response(status)
            for name, value in (extra[0] if extra else {}).items():
                self.send_header(name, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # the names http.server calls
            self._serve("GET")

        def do_POST(self):
            self._serve("POST")

        def log_message(self, format, *args):  # keep the test output quiet
            pass

    return Handler
