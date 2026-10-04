"""The hub the Playwright tests run against: a database of its own on the test Postgres, a fake GitHub, and
``evo-agents hub serve`` from this checkout, plus a small control server the tests use to sign in.

playwright.config.ts starts it as a web server and waits for GET /health on the control port, which answers only
once the API is up. SIGTERM or SIGINT stops the API and the fake and drops the database and its role.

Signing in goes through the real web flow: the browser follows /v1/auth/web/login to the fake github.com, which
signs in whoever the ``fake_github_login`` cookie names (``login:id``) on the fake's host, the way a browser
already signed in to github.com would be. Each Playwright context sets its own cookie, so tests can run in
parallel. Seeding goes through the real API too: POST /github/token hands out a GitHub token the fake issued to the
hub's app, which POST /v1/auth/github trades for a machine token.

The blob store is moto's fake S3 (``tests.hub.s3``) in this process, never a real one: ``pg.clean_env`` drops every
EVO_HUB_S3_* variable of the shell first. The knowledge graph tests push their fixture through it, and POST /kg/seed
and /kg/build run the worker's build in this process (``kg_seed.py``).

Environment: EVO_HUB_TEST_DSN (required, a superuser DSN), E2E_API_PORT (18324), E2E_STACK_PORT (18325),
E2E_WEB_ORIGIN (http://localhost:3324, the hub's public URL), E2E_ADMIN_LOGIN (e2e-admin).
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote

ROOT = Path(__file__).resolve().parents[2]  # the evo-agents checkout this web/ belongs to
sys.path.insert(0, str(ROOT))

from kg_seed import KgSeeder  # noqa: E402

from tests.hub import pg  # noqa: E402
from tests.hub.fake_github import Account, FakeGitHub, Recorded  # noqa: E402
from tests.hub.s3 import fake_s3  # noqa: E402

LOGIN_COOKIE = "fake_github_login"
STATE_DIR = Path(__file__).resolve().parent / ".stack"
READY_TIMEOUT = 90.0


_AUTHORIZE_LOCK = threading.Lock()  # _authorize reads the shared browser_account field


class BrowserGitHub(FakeGitHub):
    """The fake GitHub, with the browser's signed-in account read from a cookie instead of one shared field."""

    def answer(self, method: str, path: str, query: dict, headers: dict, body: str):
        if method == "GET" and path == "/login/oauth/authorize":
            self.requests.append(Recorded(method, path, query, headers, body))
            return self._authorize_as(query, _cookie_account(headers.get("cookie", "")))
        return super().answer(method, path, query, headers, body)

    def _authorize_as(self, query: dict, account: Account | None):
        if account is None:
            return 403, {"error": f"nobody is signed in to the fake github.com: set the {LOGIN_COOKIE} cookie"}
        with _AUTHORIZE_LOCK:
            saved, self.browser_account = self.browser_account, account
            try:
                return self._authorize(query)
            finally:
                self.browser_account = saved


def _cookie_account(header: str) -> Account | None:
    for part in header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == LOGIN_COOKIE and value:
            login, _, github_id = unquote(value).partition(":")
            if login and github_id.isdigit():
                return Account(login, int(github_id))
    return None


def create_database(admin_dsn: str):
    """A database and owner role of its own, named for this stack (pytest's own databases are left alone)."""
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    name = f"evo_hub_e2e_{secrets.token_hex(6)}"
    password = f"Pw{secrets.token_hex(16)}"
    with pg.admin(admin_dsn) as conn:
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(name), sql.Literal(password)))
        conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(name), sql.Identifier(name)))
    params = conninfo_to_dict(admin_dsn)
    host = quote(params.get("host") or "localhost", safe="")
    port = params.get("port") or "5432"
    dsn = f"postgresql://{name}:{password}@{host}:{port}/{name}"
    return pg.Database(name, password, dsn, make_conninfo(admin_dsn, dbname=name))


def drop_database(admin_dsn: str, db) -> None:
    from psycopg import sql

    with pg.admin(admin_dsn) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(db.name)))
        conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(db.name)))


def wait_for(url: str, proc: subprocess.Popen, log_path: Path) -> None:
    deadline = time.monotonic() + READY_TIMEOUT
    while True:
        if proc.poll() is not None:
            raise RuntimeError(f"hub serve exited with {proc.returncode}:\n{log_path.read_text(encoding='utf-8')}")
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        if time.monotonic() > deadline:
            raise RuntimeError(f"hub serve did not answer {url} within {READY_TIMEOUT:.0f}s")
        time.sleep(0.2)


def control_server(port: int, github: BrowserGitHub, info: dict, seeder: KgSeeder) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status: int, payload: dict) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # the names http.server calls
            if self.path == "/health":
                self._reply(200, info)
            else:
                self._reply(404, {"error": "not found"})

        def do_POST(self):
            length = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/github/token":
                token = github.issue_token(Account(str(body["login"]), int(body["id"])))
                self._reply(200, {"token": token})
            elif self.path in ("/kg/seed", "/kg/build"):
                try:
                    self._reply(200, seeder.handle(self.path, body))
                except Exception as exc:  # the test shows what failed
                    self._reply(500, {"error": f"{type(exc).__name__}: {exc}"})
            else:
                self._reply(404, {"error": "not found"})

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def main() -> int:
    admin_dsn = os.environ.get("EVO_HUB_TEST_DSN", "").strip()
    if not admin_dsn:
        print("hub_stack: EVO_HUB_TEST_DSN is not set (a superuser DSN of a test Postgres)", file=sys.stderr)
        return 2
    api_port = int(os.environ.get("E2E_API_PORT", "18324"))
    stack_port = int(os.environ.get("E2E_STACK_PORT", "18325"))
    web_origin = os.environ.get("E2E_WEB_ORIGIN", "http://localhost:3324").rstrip("/")
    admin_login = os.environ.get("E2E_ADMIN_LOGIN", "e2e-admin")

    stopping = threading.Event()

    def stop(signum, frame):
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    STATE_DIR.mkdir(exist_ok=True)
    log_path = STATE_DIR / "hub-serve.log"
    db = create_database(admin_dsn)
    github = BrowserGitHub()
    resources = ExitStack()
    proc = None
    control = None
    try:
        github.__enter__()
        blobs = resources.enter_context(fake_s3())
        env = pg.clean_env(
            PYTHONPATH=os.pathsep.join(filter(None, [str(ROOT), os.environ.get("PYTHONPATH")])),
            EVO_HUB_DSN=db.dsn,
            EVO_HUB_DATA_DIR=str(STATE_DIR / "cache"),
            EVO_HUB_ADMINS=admin_login,
            EVO_HUB_GITHUB_CLIENT_ID=github.client_id,
            EVO_HUB_GITHUB_CLIENT_SECRET=github.client_secret,
            EVO_HUB_GITHUB_URL=github.url,
            EVO_HUB_GITHUB_API_URL=github.url,
            EVO_HUB_SESSION_SECRET="e2e-session-" + secrets.token_hex(24),
            EVO_HUB_PUBLIC_URL=web_origin,
            **blobs.env(),
        )
        command = [sys.executable, "-m", "evo_agents", "hub", "serve", "--host", "127.0.0.1", "--port", str(api_port)]
        with open(log_path, "w", encoding="utf-8") as log_file:
            proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log_file, stderr=subprocess.STDOUT)
        api_url = f"http://127.0.0.1:{api_port}"
        wait_for(f"{api_url}/v1/health/live", proc, log_path)
        info = {"api": api_url, "github": github.url, "admin": admin_login, "database": db.name}
        seeder = KgSeeder(api_url, github, db.dsn, blobs.config(), STATE_DIR / "kg")
        control = control_server(stack_port, github, info, seeder)
        threading.Thread(target=control.serve_forever, daemon=True).start()
        print(f"hub_stack ready: api {api_url}, fake github {github.url}, db {db.name}, log {log_path}", flush=True)
        while not stopping.wait(0.5):
            if proc.poll() is not None:
                print(f"hub_stack: hub serve exited with {proc.returncode}, see {log_path}", file=sys.stderr)
                return 1
        return 0
    finally:
        if control is not None:
            control.shutdown()
            control.server_close()
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        github.stop()
        resources.close()
        drop_database(admin_dsn, db)
        print(f"hub_stack stopped, dropped {db.name}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
