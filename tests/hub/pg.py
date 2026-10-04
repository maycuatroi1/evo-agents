"""Postgres for the hub tests.

EVO_HUB_TEST_DSN names a server and a superuser on it; without it every hub test module skips. Each test
gets a database of its own, owned by a role of its own with a random password, so the hub runs without
superuser rights, a re-run starts clean, nothing lands in the server's own databases, and a password that
reaches a log line is easy to find. psycopg is imported inside the helpers: this module loads on a core
install too.
"""

import json
import os
import secrets
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from urllib.parse import quote

import pytest

DSN = os.environ.get("EVO_HUB_TEST_DSN", "").strip()
SKIP_REASON = "EVO_HUB_TEST_DSN is not set: hub tests need Postgres"
READY_TIMEOUT = 30.0
SERVER_STACK = (
    "fastapi",
    "uvicorn",
    "psycopg",
    "psycopg_pool",
    "alembic",
    "sqlalchemy",
    "httpx",
    "boto3",
    "procrastinate",
    "moto",
)
# Tables of migration 0004: the blob store's and procrastinate's job queue
BLOB_TABLES = frozenset({"blobs", "blob_uploads"})
QUEUE_TABLES = frozenset(
    {"procrastinate_jobs", "procrastinate_events", "procrastinate_periodic_defers", "procrastinate_workers"}
)
# Tables of migration 0006: knowledge graphs on the hub
KG_TABLES = frozenset({"kg_configs", "kg_pending_runs", "kg_builds"})
HUB_ENV = ("EVO_HUB_",)  # variables a test environment must not inherit from the shell running pytest


@dataclass(frozen=True)
class Database:
    name: str
    password: str
    dsn: str  # postgresql:// URI of the owner role, what the hub gets
    admin_dsn: str  # the superuser, connected to this database


def admin(dsn: str = DSN):
    import psycopg

    return psycopg.connect(dsn, autocommit=True, connect_timeout=5)


def wait_ready(timeout: float = READY_TIMEOUT) -> None:
    """Return once the server answers a query; fail the session after ``timeout`` seconds."""
    import psycopg

    deadline = time.monotonic() + timeout
    while True:
        try:
            with admin() as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.OperationalError as exc:
            if time.monotonic() >= deadline:
                pytest.fail(f"Postgres at EVO_HUB_TEST_DSN not ready after {timeout:.0f}s: {exc}")
            time.sleep(0.5)


def create_database() -> Database:
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    name = f"evo_hub_test_{secrets.token_hex(6)}"
    password = f"Pw{secrets.token_hex(16)}"
    with admin() as conn:
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(name), sql.Literal(password)))
        conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(name), sql.Identifier(name)))
    params = conninfo_to_dict(DSN)
    host = quote(params.get("host") or "localhost", safe="")
    port = params.get("port") or "5432"
    dsn = f"postgresql://{name}:{password}@{host}:{port}/{name}"
    return Database(name, password, dsn, make_conninfo(DSN, dbname=name))


def drop_database(db: Database) -> None:
    from psycopg import sql

    with admin() as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(db.name)))
        conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(db.name)))


def backends(name: str) -> int:
    with admin() as conn:
        return conn.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = %s", (name,)).fetchone()[0]


def wait_no_backends(name: str, timeout: float = 5.0) -> int:
    """Connections still open to the database after ``timeout``; a backend exits shortly after its client."""
    deadline = time.monotonic() + timeout
    while (left := backends(name)) and time.monotonic() < deadline:
        time.sleep(0.1)
    return left


def set_reachable(db: Database, reachable: bool) -> None:
    """Stop the database for its clients (refuse new connections, end the open ones), or bring it back:
    Postgres going away, without stopping a server other tests share."""
    from psycopg import sql

    with admin() as conn:
        allow = sql.SQL("true" if reachable else "false")
        conn.execute(sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS {}").format(sql.Identifier(db.name), allow))
        if not reachable:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                (db.name,),
            )


def free_port() -> int:
    """A port nothing listens on right now."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(HUB_ENV) and k != "PGPASSWORD"}
    env.update(extra)
    return env


def cli(args: list[str], env: dict[str, str], timeout: float = 120) -> subprocess.CompletedProcess:
    """``evo-agents ARGS`` in a process of its own."""
    return subprocess.run(
        [sys.executable, "-m", "evo_agents", *args], env=env, capture_output=True, text=True, timeout=timeout
    )


def log_lines(stderr: str) -> list[dict]:
    """The JSON object of every stderr line; fails the test on a line that is not one."""
    lines = []
    for line in stderr.splitlines():
        if line.strip():
            try:
                lines.append(json.loads(line))
            except ValueError:
                pytest.fail(f"stderr line is not a JSON object: {line!r}")
    return lines
