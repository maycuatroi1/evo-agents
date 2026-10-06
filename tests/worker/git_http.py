"""git's smart HTTP on 127.0.0.1 behind Basic auth, for the tests of a run's credentials: a server in a thread of the
test's process runs ``git http-backend`` as a CGI program for each request, over the bare repositories under a root.

A request without the expected user and password gets 401 with a Basic challenge, as GitLab answers one, so git asks
its credential helpers and tries again with what they answered. ``requests`` keeps (method, path, user or None,
status) of each request, never the password. Pushing needs no configuration of the repositories: http-backend takes
a push from a request that names REMOTE_USER, which this server sets once the user is checked.
"""

from __future__ import annotations

import base64
import binascii
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

CGI_TIMEOUT = 60


class GitHttp:
    """``git http-backend`` for the bare repositories under ``root``, for ``username`` with ``password`` alone."""

    def __init__(self, root: Path, username: str, password: str):
        self.root = Path(root)
        self.username = username
        self.password = password
        self.requests: list[tuple[str, str, str | None, int]] = []
        self.url = ""
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> GitHttp:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def start(self) -> str:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.daemon_threads = True
        server.owner = self
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, name="git-http-backend", daemon=True)
        self._thread.start()
        self.url = f"http://127.0.0.1:{server.server_address[1]}"
        return self.url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def repo_url(self, name: str) -> str:
        return f"{self.url}/{name}"

    def user_of(self, header: str | None) -> str | None:
        """The user a Basic Authorization header proves, or None."""
        scheme, _, encoded = (header or "").partition(" ")
        if scheme.lower() != "basic":
            return None
        try:
            user, _, password = base64.b64decode(encoded.strip()).decode().partition(":")
        except (binascii.Error, UnicodeDecodeError):
            return None
        return user if (user, password) == (self.username, self.password) else None

    def statuses(self, method: str | None = None) -> list[int]:
        return [status for verb, _, _, status in self.requests if method is None or verb == method]


class _Handler(BaseHTTPRequestHandler):
    server_version = "git-http-test"

    def log_message(self, format, *args) -> None:  # noqa: A002 - the name BaseHTTPRequestHandler gives it
        pass

    def do_GET(self) -> None:  # noqa: N802
        self._serve()

    def do_POST(self) -> None:  # noqa: N802
        self._serve()

    def _body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            data = bytearray()
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    self.rfile.readline()
                    return bytes(data)
                data += self.rfile.read(size)
                self.rfile.readline()
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _serve(self) -> None:
        owner: GitHttp = self.server.owner
        path, _, query = self.path.partition("?")
        user = owner.user_of(self.headers.get("Authorization"))
        if user is None:
            owner.requests.append((self.command, path, None, 401))
            self._body()
            body = b"authentication required\n"
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="git"')
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        data = self._body()
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(owner.root),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_PROJECT_ROOT": str(owner.root),
            "GIT_HTTP_EXPORT_ALL": "1",
            "REQUEST_METHOD": self.command,
            "PATH_INFO": unquote(path),
            "QUERY_STRING": query,
            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(len(data)),
            "REMOTE_USER": user,
            "REMOTE_ADDR": "127.0.0.1",
        }
        if self.headers.get("Content-Encoding"):
            env["HTTP_CONTENT_ENCODING"] = self.headers["Content-Encoding"]
        if self.headers.get("Git-Protocol"):
            env["GIT_PROTOCOL"] = self.headers["Git-Protocol"]
        done = subprocess.run(["git", "http-backend"], input=data, env=env, capture_output=True, timeout=CGI_TIMEOUT)
        head, sep, body = done.stdout.partition(b"\r\n\r\n")
        if not sep:
            head, _, body = done.stdout.partition(b"\n\n")
        status, headers = 200, []
        for line in head.decode("latin-1").splitlines():
            name, _, value = line.partition(":")
            if name.strip().lower() == "status":
                status = int(value.strip().split()[0])
            elif name.strip():
                headers.append((name.strip(), value.strip()))
        owner.requests.append((self.command, path, user, status))
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
