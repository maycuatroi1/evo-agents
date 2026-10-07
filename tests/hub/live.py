"""Helpers for the sign-in tests: ``evo-agents hub serve`` in a process of its own for the CLI to talk to, the
in-process app wired to a fake GitHub, and rows put straight into a test database.

The server stack is imported inside the helpers: this module loads on a core install too.
"""

from __future__ import annotations

import json
import secrets
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from tests.hub import pg

ADMIN = "octo-admin"
LEVELS = ["public", "internal", "customer", "secret"]


@dataclass
class LiveHub:
    url: str
    proc: subprocess.Popen
    log_path: Path

    def log(self) -> str:
        return self.log_path.read_text(encoding="utf-8")


@contextmanager
def running_hub(db: pg.Database, tmp_path: Path, **variables: str):
    """``hub serve`` on a free port with ``variables`` added to a clean environment; its stdout and stderr go
    to a file, readable while it runs and after it stopped."""
    port = pg.free_port()
    log_path = tmp_path / "hub-serve.log"
    env = pg.clean_env(EVO_HUB_DSN=db.dsn, EVO_HUB_DATA_DIR=str(tmp_path / "cache"), **variables)
    command = [sys.executable, "-m", "evo_agents", "hub", "serve", "--host", "127.0.0.1", "--port", str(port)]
    with open(log_path, "w", encoding="utf-8") as log_file:
        proc = subprocess.Popen(command, env=env, stdout=log_file, stderr=subprocess.STDOUT)
    hub = LiveHub(f"http://127.0.0.1:{port}", proc, log_path)
    try:
        deadline = time.monotonic() + 60
        while True:
            assert proc.poll() is None, hub.log()
            try:
                with urllib.request.urlopen(f"{hub.url}/v1/health/live", timeout=2) as response:
                    if response.status == 200:
                        break
            except OSError:
                assert time.monotonic() < deadline, "hub serve did not answer within 60s"
                time.sleep(0.2)
        yield hub
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def session_secret() -> str:
    return "session-secret-" + secrets.token_hex(24)


def hub_config(db: pg.Database, tmp_path: Path, github, **changes):
    """The configuration of an in-process hub on ``db`` that signs in through the fake ``github``: device flow
    only, unless ``changes`` add the client secret, the session secret and the public URL."""
    from evo_agents.hub.config import HubConfig

    values = {
        "dsn": db.dsn,
        "data_dir": tmp_path / "cache",
        "pool_min_size": 1,
        "pool_max_size": 4,
        "pool_timeout": 5.0,
        "admins": frozenset({ADMIN}),
        "github_client_id": github.client_id,
        "github_url": github.url,
        "github_api_url": github.url,
        "github_timeout": 2.0,
    }
    values.update(changes)
    return HubConfig(**values)


def web_changes(github) -> dict:
    """What ``hub_config`` needs on top for the web sign-in."""
    return {
        "github_client_secret": github.client_secret,
        "session_secret": session_secret(),
        "public_url": "https://hub.test",
    }


def sign_in(client, github, login: str, github_id: int, host: str = "laptop") -> dict:
    """POST /v1/auth/github with a fresh GitHub token of the account, as the CLI does after the device flow."""
    from tests.hub.fake_github import Account

    token = github.issue_token(Account(login, github_id))
    response = client.post("/v1/auth/github", json={"github_token": token, "host": host})
    assert response.status_code == 201, response.text
    return response.json()


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def engine(db: pg.Database):
    """A SQLAlchemy engine on ``db`` as its owner, the role the hub runs as. It keeps no connection (``NullPool``):
    each ``begin()`` connects, and closes once its block ends, so nothing outlives a test."""
    if db.dsn not in _ENGINES:
        import psycopg
        from sqlalchemy import create_engine
        from sqlalchemy.pool import NullPool

        _ENGINES[db.dsn] = create_engine(
            "postgresql+psycopg://", creator=lambda: psycopg.connect(db.dsn), poolclass=NullPool
        )
    return _ENGINES[db.dsn]


_ENGINES: dict = {}


def sql(db: pg.Database, statement, params=()):
    """Run one statement in a transaction of its own; its rows as tuples, or None when it returns none. A Core
    statement (on ``evo_agents.hub.tables``) runs on ``engine(db)``, with ``params`` as a dict or a list of dicts,
    and a refusal raises the driver's error, such as ``psycopg.errors.CheckViolation``; a string runs as the
    superuser, until no test passes one."""
    if not isinstance(statement, str):
        from sqlalchemy.exc import DBAPIError

        try:
            with engine(db).begin() as conn:
                result = conn.execute(statement, params or None)
                return [tuple(row) for row in result] if result.returns_rows else None
        except DBAPIError as exc:
            raise exc.orig from exc
    with pg.admin(db.admin_dsn) as conn:
        cursor = conn.execute(statement, params)
        return cursor.fetchall() if cursor.description else None


def add_project(db: pg.Database, name: str = "demo", levels: list[str] | None = None) -> int:
    """A registered project, created by a user of its own (projects come from step 3's registry)."""
    from psycopg.types.json import Jsonb

    with pg.admin(db.admin_dsn) as conn:
        owner = conn.execute(
            "INSERT INTO users (login) VALUES (%s) ON CONFLICT DO NOTHING RETURNING id", (f"{name}-owner",)
        ).fetchone()
        if owner is None:
            owner = conn.execute("SELECT id FROM users WHERE login = %s", (f"{name}-owner",)).fetchone()
        return conn.execute(
            "INSERT INTO projects (name, levels, locations, default_label, created_by) VALUES (%s, %s, %s, %s, %s) "
            "RETURNING id",
            (name, levels or LEVELS, ["any"], Jsonb({"level": (levels or LEVELS)[0]}), owner[0]),
        ).fetchone()[0]


def insert_token(db: pg.Database, login: str, kind: str = "machine") -> str:
    """A live token of ``login`` written straight into the database, for tests about something else."""
    from evo_agents.hub.server.security import hash_token, new_token

    token = new_token(kind)
    with pg.admin(db.admin_dsn) as conn:
        user_id = (
            conn.execute(
                "INSERT INTO users (login) VALUES (%s) ON CONFLICT DO NOTHING RETURNING id", (login,)
            ).fetchone()
            or conn.execute("SELECT id FROM users WHERE login = %s", (login,)).fetchone()
        )
        conn.execute(
            "INSERT INTO tokens (user_id, kind, token_hash, host, expires_at) "
            "VALUES (%s, %s, %s, %s, now() + interval '90 days')",
            (user_id[0], kind, hash_token(token), "test-host" if kind == "machine" else None),
        )
    return token


def table_dump(db: pg.Database) -> str:
    """Every row of every hub table as JSON text, to search for a value that must not be stored."""
    tables = [row[0] for row in sql(db, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")]
    rows = []
    for table in tables:
        rows += [row[0] for row in sql(db, f'SELECT row_to_json(t)::text FROM "{table}" t')]
    return json.dumps(rows)
