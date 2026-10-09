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


def engine(db: pg.Database, *, admin: bool = False):
    """A SQLAlchemy engine on ``db`` as its owner, the role the hub runs as, or as the superuser with ``admin``. It
    keeps no connection (``NullPool``): each ``begin()`` or ``connect()`` connects, and closes once its block ends,
    so nothing outlives a test. A statement the database refuses raises the driver's error, such as
    ``psycopg.errors.CheckViolation``, rather than SQLAlchemy's wrapper, so a test names the SQLSTATE it expects."""
    dsn = db.admin_dsn if admin else db.dsn
    if dsn not in _ENGINES:
        import psycopg
        from sqlalchemy import create_engine, event
        from sqlalchemy.pool import NullPool

        made = create_engine("postgresql+psycopg://", creator=lambda: psycopg.connect(dsn), poolclass=NullPool)
        event.listen(made, "handle_error", lambda context: context.original_exception)
        _ENGINES[dsn] = made
    return _ENGINES[dsn]


_ENGINES: dict = {}


@contextmanager
def connect(db: pg.Database, *, admin: bool = False):
    """A connection of ``engine(db)`` in autocommit, as ``pg.admin`` gave: each statement commits on its own, and one
    the database refuses leaves the connection usable for the next."""
    with engine(db, admin=admin).connect() as conn:
        yield conn.execution_options(isolation_level="AUTOCOMMIT")


def sql(db: pg.Database, statement, params=()):
    """Run one Core statement (on ``evo_agents.hub.tables``) on ``engine(db)``, in a transaction of its own, with
    ``params`` as a dict or a list of dicts; its rows as tuples, or None when it returns none. A refusal raises the
    driver's error, such as ``psycopg.errors.CheckViolation``."""
    with engine(db).begin() as conn:
        result = conn.execute(statement, params or None)
        return [tuple(row) for row in result] if result.returns_rows else None


def _user_id(conn, login: str) -> int:
    """The id of the user ``login``, added when there is none."""
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert

    from evo_agents.hub import tables

    users = tables.users
    added = conn.execute(insert(users).values(login=login).on_conflict_do_nothing().returning(users.c.id)).scalar()
    return added if added is not None else conn.execute(select(users.c.id).where(users.c.login == login)).scalar_one()


def add_project(db: pg.Database, name: str = "demo", levels: list[str] | None = None) -> int:
    """A registered project, created by a user of its own (projects come from step 3's registry)."""
    from sqlalchemy import insert

    from evo_agents.hub import tables

    projects = tables.projects
    with engine(db).begin() as conn:
        owner = _user_id(conn, f"{name}-owner")
        project = insert(projects).values(
            name=name,
            levels=levels or LEVELS,
            locations=["any"],
            default_label={"level": (levels or LEVELS)[0]},
            created_by=owner,
        )
        return conn.execute(project.returning(projects.c.id)).scalar_one()


def insert_token(db: pg.Database, login: str, kind: str = "machine") -> str:
    """A live token of ``login`` written straight into the database, for tests about something else."""
    from datetime import timedelta

    from sqlalchemy import func, insert

    from evo_agents.hub import tables
    from evo_agents.hub.server.security import hash_token, new_token

    token = new_token(kind)
    with engine(db).begin() as conn:
        conn.execute(
            insert(tables.tokens).values(
                user_id=_user_id(conn, login),
                kind=kind,
                token_hash=hash_token(token),
                host="test-host" if kind == "machine" else None,
                expires_at=func.now() + timedelta(days=90),
            )
        )
    return token


def web_session(db: pg.Database, login: str) -> tuple[str, int]:
    """A live web session of ``login`` written straight into the database: its cookie value and its id."""
    from sqlalchemy import select

    from evo_agents.hub import tables
    from evo_agents.hub.server.security import WEB, hash_token

    token = insert_token(db, login, kind=WEB)
    found = sql(db, select(tables.tokens.c.id).where(tables.tokens.c.token_hash == hash_token(token)))
    return token, found[0][0]


def table_dump(db: pg.Database) -> str:
    """Every row of every hub table as JSON text, to search for a value that must not be stored."""
    from sqlalchemy import Text, cast, column, func, select, table

    catalog = table("pg_tables", column("schemaname", Text), column("tablename", Text))
    names = [row[0] for row in sql(db, select(catalog.c.tablename).where(catalog.c.schemaname == "public"))]
    rows = []
    for name in names:
        each = table(name).alias("t")
        rows += [row[0] for row in sql(db, select(cast(func.row_to_json(each.table_valued()), Text)))]
    return json.dumps(rows)
