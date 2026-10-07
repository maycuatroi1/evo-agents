"""The hub client behind ``evo-agents hub login``, ``logout``, ``whoami``, ``token`` and ``admin``.

Standard library only: the core package keeps PyYAML as its only dependency. Credentials live in ~/.evo/hub, where
``config.json`` holds ``{url, login}`` and ``token`` the machine token; the directory is mode 0700 and both files
0600. Each file is written to a temporary file in the same directory, flushed to disk and renamed over the old
one, so a crash leaves the old file or the new one, never half a token.

Requests carry the token as a Bearer header and never follow a redirect, which would hand the header to whatever
host the redirect names. A hub URL must be https, or http to a loopback address for local runs. Every request has
a timeout, and a hub or a GitHub that does not answer is an error naming its URL.

``Connections`` keeps HTTP(S) connections to hubs open between requests, for callers that send many small ones, such
as ``evo-agents hub mcp`` (``evo_agents.hub.mcp_proxy``): a TLS handshake costs more than the request itself. It sends
the same requests ``_send`` does, answers and fails the same way, and falls back to ``_send`` when a proxy is
configured for the hub.

Sign-in is GitHub's device flow: the CLI asks GitHub for a user code, the person enters it at the verification
URL, and the CLI polls until GitHub hands over a token, honouring ``interval`` and adding SLOW_DOWN_STEP seconds on
every ``slow_down``. The client id comes from the hub (GET /v1/auth/config). GitHub's own URL comes from
EVO_HUB_GITHUB_URL (default https://github.com), never from the hub, so a hub cannot send the person to a sign-in
page of its choosing. The GitHub token goes to the hub once, which trades it for a hub token, and is not kept.
"""

from __future__ import annotations

import contextlib
import functools
import http.client
import json
import os
import select
import socket
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from evo_agents import __version__

DEFAULT_GITHUB_URL = "https://github.com"
GITHUB_URL_VARIABLE = "EVO_HUB_GITHUB_URL"
TIMEOUT = 30.0  # seconds for one HTTP request
DEVICE_SCOPE = "read:user"  # reading the login needs nothing more
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
DEFAULT_INTERVAL = 5  # seconds between polls when GitHub does not say
SLOW_DOWN_STEP = 5  # seconds added to the interval on every slow_down
POLL_FAILURES = 3  # network failures in a row while polling before giving up
LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
IDLE_TIMEOUT = 30.0  # seconds a kept-alive connection may wait unused before it is closed rather than reused
MAX_IDLE = 8  # kept-alive connections per hub
DIR_MODE = 0o700
FILE_MODE = 0o600
LOGIN_HINT = "run `evo-agents hub login --url URL`"


class HubError(Exception):
    """A request failed; ``str`` is a sentence for the person at the terminal."""

    def __init__(self, message: str, status: int | None = None, code: str | None = None, payload=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.payload = payload  # the hub's JSON error body, such as the current version a 409 carries


class NotSignedIn(HubError):
    pass


class Unreachable(HubError):
    """The hub did not answer: it could not be reached, or no answer came in time. ``refused`` when the connection
    was refused, so the request was never sent and sending it again cannot do anything twice."""

    def __init__(self, message: str, refused: bool = False):
        super().__init__(message)
        self.refused = refused


@dataclass(frozen=True)
class Credentials:
    url: str
    login: str
    token: str
    run: int | None = None  # a worker token's, for the agent of this run (``evo_agents.hub.mcp_proxy``)

    def __repr__(self) -> str:  # the token stays out of tracebacks
        run = "" if self.run is None else f", run={self.run}"
        return f"Credentials(url={self.url!r}, login={self.login!r}{run})"


def hub_dir() -> Path:
    return Path.home() / ".evo" / "hub"


def check_url(url: str, what: str = "the hub URL") -> str:
    """``url`` without a trailing slash; https, or http to a loopback address. Raises HubError otherwise."""
    url = (url or "").strip().rstrip("/")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.query or parts.fragment:
        raise HubError(f"{what} must look like https://hub.example.org, not {url!r}")
    if parts.scheme == "http" and parts.hostname not in LOOPBACK:
        raise HubError(f"{what} must use https: a token sent to {parts.hostname} over http can be read on the way")
    return url


# Credentials on disk


def _private_dir() -> Path:
    directory = hub_dir()
    directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    os.chmod(directory, DIR_MODE)  # mkdir's mode goes through the umask, and an older directory may be wider
    return directory


def _fsync_dir(directory: Path) -> None:
    with contextlib.suppress(OSError):  # not every platform can open a directory
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def write_private(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` atomically, mode 0600 from the first byte on."""
    write_atomic(path, data, FILE_MODE)


def write_atomic(path: Path, data: bytes, mode: int) -> None:
    """Replace ``path`` with ``data`` atomically: a temporary file in the same directory, flushed to disk and
    renamed over it, so a crash leaves the old file or the new one. ``mode`` exactly, whatever the umask."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")  # created 0600
    try:
        os.chmod(tmp, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    _fsync_dir(path.parent)


def save_credentials(url: str, login: str, token: str) -> Path:
    directory = _private_dir()
    write_private(directory / "token", token.encode() + b"\n")
    write_private(directory / "config.json", json.dumps({"url": url, "login": login}, indent=2).encode() + b"\n")
    return directory


def load_credentials() -> Credentials:
    directory = hub_dir()
    try:
        config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
        token = (directory / "token").read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise NotSignedIn(f"not signed in to a hub: {LOGIN_HINT}") from None
    except (OSError, ValueError) as exc:
        raise HubError(f"cannot read the credentials in {directory} ({exc}): {LOGIN_HINT}") from None
    url, login = (config.get("url"), config.get("login")) if isinstance(config, dict) else (None, None)
    if not isinstance(url, str) or not isinstance(login, str) or not token:
        raise HubError(f"the credentials in {directory} are incomplete: {LOGIN_HINT}")
    return Credentials(url, login, token)


def remove_credentials() -> None:
    directory = hub_dir()
    for name in ("token", "config.json"):
        (directory / name).unlink(missing_ok=True)


# HTTP


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then raises HTTPError with the 3xx status, handled below


_OPENER = urllib.request.build_opener(_NoRedirect)


def _origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _send(method: str, url: str, headers: dict, data: bytes | None, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, exc.read()
    except urllib.error.URLError as exc:
        refused = isinstance(exc.reason, ConnectionRefusedError)
        raise Unreachable(f"cannot reach {_origin(url)}: {exc.reason}", refused) from None
    except (TimeoutError, OSError, http.client.HTTPException) as exc:
        reason = f"no answer within {timeout:g}s" if isinstance(exc, TimeoutError) else str(exc) or type(exc).__name__
        raise Unreachable(f"cannot reach {_origin(url)}: {reason}") from None


@functools.lru_cache(maxsize=32)
def _proxied(scheme: str, host: str) -> bool:
    """Whether urllib would send a request for ``host`` through a proxy (the environment's, or the system's)."""
    return scheme in urllib.request.getproxies() and not urllib.request.proxy_bypass(host)


def _dropped(sock) -> bool:
    """Whether an idle connection's socket has something to read: the other end closed it (or sent what nobody asked
    for), so it cannot carry another request."""
    try:
        if isinstance(sock, ssl.SSLSocket) and sock.pending():
            return True
        if hasattr(select, "poll"):
            poller = select.poll()
            poller.register(sock, select.POLLIN | select.POLLPRI)
            return bool(poller.poll(0))
        return bool(select.select([sock], [], [], 0)[0])
    except (OSError, ValueError):
        return True


class Connections:
    """Kept-alive connections to hubs, shared by the threads of a process: each request takes one to itself, and
    gives it back once the answer is read whole, unless either side asked to close it.

    A connection idle for over ``idle_timeout`` seconds, or that the hub closed meanwhile, is closed instead of
    reused, so a hub that restarted is reached on a new connection. A request whose sending fails on a reused
    connection is sent once more on a new one: the hub closed the connection before it could read the request whole.
    Once a request is sent, it is never sent again, whatever happens to its answer. Errors are those of ``_send``:
    ``Unreachable`` (``refused`` when connecting was refused) for a hub that gives no answer."""

    def __init__(self, idle_timeout: float = IDLE_TIMEOUT, max_idle: int = MAX_IDLE, clock=time.monotonic):
        self.idle_timeout = idle_timeout
        self.max_idle = max_idle
        self.clock = clock
        self.opened = 0  # connections opened so far
        self._lock = threading.Lock()
        self._idle: dict[tuple, list[tuple[float, http.client.HTTPConnection]]] = {}

    def close(self) -> None:
        with self._lock:
            idle, self._idle = self._idle, {}
        for connections in idle.values():
            for _, conn in connections:
                conn.close()

    def _take(self, key: tuple) -> http.client.HTTPConnection | None:
        """The connection to ``key`` used last that can carry a request, or None."""
        while True:
            with self._lock:
                idle = self._idle.get(key)
                if not idle:
                    return None
                since, conn = idle.pop()
            if self.clock() - since <= self.idle_timeout and conn.sock is not None and not _dropped(conn.sock):
                return conn
            conn.close()

    def _give_back(self, key: tuple, conn: http.client.HTTPConnection) -> None:
        with self._lock:
            idle = self._idle.setdefault(key, [])
            if len(idle) < self.max_idle:
                idle.append((self.clock(), conn))
                return
        conn.close()

    def _open(self, scheme: str, host: str, port: int | None, timeout: float) -> http.client.HTTPConnection:
        kind = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        conn = kind(host, port, timeout=timeout)
        conn.connect()
        with self._lock:
            self.opened += 1
        return conn

    def send(self, method: str, url: str, headers: dict, data: bytes | None, timeout: float) -> tuple[int, bytes]:
        """The status and body of ``method`` on ``url``, as ``_send`` answers them, on a kept-alive connection."""
        parts = urllib.parse.urlsplit(url)
        if _proxied(parts.scheme, parts.hostname or ""):
            return _send(method, url, headers, data, timeout)
        key = (parts.scheme, parts.hostname, parts.port)
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        conn = self._take(key)
        reused = conn is not None
        while True:
            try:
                if conn is None:
                    conn = self._open(parts.scheme, parts.hostname, parts.port, timeout)
                else:
                    conn.sock.settimeout(timeout)
                conn.request(method, target, body=data, headers=headers)
                break
            except (OSError, http.client.HTTPException) as exc:
                if conn is not None:
                    conn.close()
                if reused:  # closed by the hub while it was idle: the request never reached it whole
                    conn, reused = None, False
                    continue
                refused = isinstance(exc, ConnectionRefusedError)
                raise Unreachable(f"cannot reach {_origin(url)}: {exc}", refused) from None
        try:
            response = conn.getresponse()
            body = response.read()
        except (TimeoutError, OSError, http.client.HTTPException) as exc:
            conn.close()
            reason = (
                f"no answer within {timeout:g}s" if isinstance(exc, TimeoutError) else str(exc) or type(exc).__name__
            )
            raise Unreachable(f"cannot reach {_origin(url)}: {reason}") from None
        if response.will_close:
            conn.close()
        else:
            self._give_back(key, conn)
        return response.status, body


CONNECTIONS = Connections()  # the process's kept-alive connections to hubs


def _json(raw: bytes):
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def put_presigned(url: str, path: Path, timeout: float = TIMEOUT) -> None:
    """PUT the file at ``path``, its exact bytes, to a presigned URL the hub handed out. The URL is a credential until
    it expires: an error names its host only."""
    size = path.stat().st_size
    headers = {
        "Content-Type": "application/octet-stream",
        "Content-Length": str(size),
        "User-Agent": f"evo-agents/{__version__}",
    }
    with open(path, "rb") as handle:
        status, _ = _send("PUT", url, headers, handle, timeout)
    if not 200 <= status < 300:
        raise HubError(f"the blob store at {_origin(url)} answered HTTP {status} to an upload", status)


class Hub:
    """Calls to one hub, as the holder of ``token`` when one is given."""

    def __init__(self, url: str, token: str | None = None, timeout: float = TIMEOUT):
        self.url = check_url(url)
        self.token = token
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"Hub({self.url!r})"

    def call(self, method: str, path: str, body: dict | None = None, *, timeout: float | None = None):
        """The decoded JSON answer, None for an empty one; HubError with the hub's message otherwise. ``timeout``
        replaces the hub's own for this request, for one whose work grows with its size, such as a blob commit."""
        headers = {"Accept": "application/json", "User-Agent": f"evo-agents/{__version__}"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        status, raw = _send(method, self.url + path, headers, data, self.timeout if timeout is None else timeout)
        payload = _json(raw)
        if 300 <= status < 400:
            raise HubError(f"the hub at {self.url} answered {status} with a redirect, which is not followed", status)
        if status >= 400:
            message = payload.get("message") if isinstance(payload, dict) else None
            code = payload.get("error") if isinstance(payload, dict) else None
            raise HubError(str(message or f"the hub at {self.url} answered HTTP {status}"), status, code, payload)
        return payload


# GitHub's device flow


@dataclass(frozen=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


def github_url(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return check_url(env.get(GITHUB_URL_VARIABLE) or DEFAULT_GITHUB_URL, GITHUB_URL_VARIABLE)


def _github_post(base: str, path: str, form: dict) -> tuple[int, dict]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": f"evo-agents/{__version__}",
    }
    status, raw = _send("POST", base + path, headers, urllib.parse.urlencode(form).encode(), TIMEOUT)
    payload = _json(raw)
    if status >= 500 or not isinstance(payload, dict):
        raise HubError(f"GitHub at {base} answered HTTP {status} to {path}; try again shortly", status)
    return status, payload


def _seconds(value, default: int) -> int:
    return value if type(value) is int and value >= 0 else default


def start_device_flow(base: str, client_id: str) -> DeviceCode:
    status, data = _github_post(base, "/login/device/code", {"client_id": client_id, "scope": DEVICE_SCOPE})
    fields = (data.get("device_code"), data.get("user_code"), data.get("verification_uri"))
    if status != 200 or not all(isinstance(value, str) and value for value in fields):
        error = data.get("error") or f"HTTP {status}"
        raise HubError(f"GitHub did not start the device flow ({error}): the hub's OAuth App must allow device flow")
    return DeviceCode(
        *fields,
        expires_in=_seconds(data.get("expires_in"), 900),
        interval=_seconds(data.get("interval"), DEFAULT_INTERVAL),
    )


def wait_for_token(
    base: str,
    client_id: str,
    device: DeviceCode,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Poll GitHub until the person has entered the code; the GitHub token, or HubError saying why not."""
    interval = device.interval
    deadline = clock() + device.expires_in
    failures = 0
    form = {"client_id": client_id, "device_code": device.device_code, "grant_type": DEVICE_GRANT}
    while True:
        if clock() + interval > deadline:
            raise HubError("the code expired before it was entered on GitHub: run `evo-agents hub login` again")
        sleep(interval)
        try:
            status, data = _github_post(base, "/login/oauth/access_token", form)
        except HubError:
            failures += 1
            if failures >= POLL_FAILURES:
                raise
            continue
        failures = 0
        token = data.get("access_token")
        if isinstance(token, str) and token:
            return token
        error = data.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval = max(interval + SLOW_DOWN_STEP, _seconds(data.get("interval"), 0))
            continue
        if error == "expired_token":
            raise HubError("the code expired before it was entered on GitHub: run `evo-agents hub login` again")
        if error == "access_denied":
            raise HubError("the sign-in was cancelled on GitHub")
        raise HubError(f"GitHub refused the device flow ({error or f'HTTP {status}'})", status, error)


def host_name() -> str:
    """This machine's name as token lists show it."""
    name = "".join(ch for ch in socket.gethostname() if ch.isprintable()).strip()
    return name[:255] or "unknown-host"


def login(
    url: str,
    *,
    out=None,
    env: Mapping[str, str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    """Sign in to the hub at ``url`` through GitHub's device flow and save the credentials; the hub's answer
    without the token. A previous token of this machine for the same hub is revoked once the new one is saved."""
    out = out or sys.stdout
    hub = Hub(url)
    base = github_url(env)
    config = hub.call("GET", "/v1/auth/config")
    client_id = config.get("github_client_id") if isinstance(config, dict) else None
    if not isinstance(client_id, str) or not client_id:
        raise HubError(f"the hub at {hub.url} did not give a GitHub client id")
    device = start_device_flow(base, client_id)
    print(f"First copy your one-time code: {device.user_code}", file=out)
    print(f"Then open {device.verification_uri} and enter it. Waiting for GitHub...", file=out, flush=True)
    github_token = wait_for_token(base, client_id, device, sleep=sleep, clock=clock)
    signed = hub.call("POST", "/v1/auth/github", {"github_token": github_token, "host": host_name()})
    if not isinstance(signed, dict) or not isinstance(signed.get("token"), str) or not signed.get("login"):
        raise HubError(f"the hub at {hub.url} did not answer the sign-in with a token")
    try:
        previous = load_credentials()
    except HubError:
        previous = None
    save_credentials(hub.url, signed["login"], signed["token"])
    if previous is not None and previous.url == hub.url and previous.token != signed["token"]:
        with contextlib.suppress(HubError):  # revoked or expired already, or the hub is busy: it expires unused
            Hub(hub.url, previous.token).call("POST", "/v1/auth/logout")
    return {key: value for key, value in signed.items() if key != "token"}
