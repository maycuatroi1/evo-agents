"""Postgres for the hub tests.

EVO_HUB_TEST_DSN names a server and a superuser on it; without it every hub test module skips. Each test
gets a database of its own, owned by a role of its own with a random password, so the hub runs without
superuser rights, a re-run starts clean, nothing lands in the server's own databases, and a password that
reaches a log line is easy to find. Such a database is a copy of a template the pytest process migrated once
(``make_template``, ``clone_database``): a copy takes about a quarter of the time of migrating an empty database,
which some 900 tests did each; a test of migrating itself gets an empty one. The tests run in parallel with
``pytest -n 4`` (pytest-xdist): each worker has a template of its own, and nothing else is shared but the server.
psycopg and SQLAlchemy are imported inside the helpers: this module loads on a core install too.

The SQL kept here is the CREATE, ALTER and DROP of databases and roles and the REASSIGN OWNED of a copy, which have no
SQLAlchemy construct; the catalogs it reads and the functions it calls are Core statements on ``admin_engine()``.
"""

import json
import os
import secrets
import socket
import subprocess
import sys
import threading
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
    "cryptography",
)
# Tables of migration 0004: the blob store's and procrastinate's job queue
BLOB_TABLES = frozenset({"blobs", "blob_uploads"})
QUEUE_TABLES = frozenset(
    {"procrastinate_jobs", "procrastinate_events", "procrastinate_periodic_defers", "procrastinate_workers"}
)
# Tables of migration 0006: knowledge graphs on the hub
KG_TABLES = frozenset({"kg_configs", "kg_pending_runs", "kg_builds"})
# Tables of migration 0008: blobs being deleted, such as the artifacts of old graphs
RETENTION_TABLES = frozenset({"blob_deletions"})
# Tables of migration 0009: workers, their pairings and projects, and the runs with their events and inbox
RUN_TABLES = frozenset({"workers", "worker_projects", "worker_pairings", "runs", "run_events", "run_inbox"})
# Tables of migration 0010: the decisions of plan runs, and the notifications, channels and deliveries
NOTIFICATION_TABLES = frozenset({"decisions", "notifications", "notification_channels", "notification_deliveries"})
# Tables of migration 0011: the secrets members keep, where they are bound, and the leases runs get of them
CREDENTIAL_TABLES = frozenset({"secrets", "secret_bindings", "credential_leases"})
# Tables of migration 0012: the charters of projects at every revision, and the schedules they make
CURATOR_TABLES = frozenset({"charters", "schedules"})
# Tables of migration 0013: the digests of Claude Code sessions, and the tool figures of runs
DIGEST_TABLES = frozenset({"session_digests", "run_tool_stats"})
# Tables of migration 0014: the night's figures of a project, and what its review run finds and proposes
REVIEW_TABLES = frozenset({"curator_figures", "findings", "proposals"})
# Tables of migration 0015: the Curator's morning briefs, and the codes that link a Telegram chat
BRIEF_TABLES = frozenset({"curator_briefs", "telegram_links"})
# migration 0017: the Curator's changes, and the repos whose ruleset the hub checked
CHANGE_TABLES = frozenset({"curator_changes", "curator_repo_checks"})
# migration 0018: the Curator's ledger
LEDGER_TABLES = frozenset({"curator_ledger"})
HUB_ENV = ("EVO_HUB_",)  # variables a test environment must not inherit from the shell running pytest
AWAY = "_away"  # suffix of the copy a database waits in while set_reachable keeps it from its clients
END_EVERY = 0.05  # seconds between two rounds of ending the connections to a database being copied


@dataclass(frozen=True)
class Database:
    name: str
    password: str
    dsn: str  # postgresql:// URI of the owner role, what the hub gets
    admin_dsn: str  # the superuser, connected to this database


def admin(dsn: str = DSN):
    import psycopg

    return psycopg.connect(dsn, autocommit=True, connect_timeout=5)


def admin_engine(dsn: str = DSN):
    """A SQLAlchemy engine on ``dsn`` as the superuser, in autocommit like ``admin()``. It keeps no connection
    (``NullPool``): each ``connect()`` connects, and closes once its block ends."""
    if dsn not in _ADMIN_ENGINES:
        from sqlalchemy import create_engine
        from sqlalchemy.pool import NullPool

        _ADMIN_ENGINES[dsn] = create_engine(
            "postgresql+psycopg://", creator=lambda: admin(dsn), poolclass=NullPool, isolation_level="AUTOCOMMIT"
        )
    return _ADMIN_ENGINES[dsn]


_ADMIN_ENGINES: dict = {}


def _activity():
    """The view of the server's backends, as much of it as this module reads."""
    from sqlalchemy import Text, column, table

    return table("pg_stat_activity", column("pid"), column("datname", Text))


def wait_ready(timeout: float = READY_TIMEOUT) -> None:
    """Return once the server answers a query; fail the session after ``timeout`` seconds."""
    from sqlalchemy import literal, select
    from sqlalchemy.exc import OperationalError

    deadline = time.monotonic() + timeout
    while True:
        try:
            with admin_engine().connect() as conn:
                conn.execute(select(literal(1)))
            return
        except OperationalError as exc:
            if time.monotonic() >= deadline:
                pytest.fail(f"Postgres at EVO_HUB_TEST_DSN not ready after {timeout:.0f}s: {exc.orig}")
            time.sleep(0.5)


def _new_owner(conn) -> tuple[str, str]:
    """A login role of its own with a random password; its name is also the name of its database."""
    from psycopg import sql

    name = f"evo_hub_test_{secrets.token_hex(6)}"
    password = f"Pw{secrets.token_hex(16)}"
    conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(name), sql.Literal(password)))
    return name, password


def _database(name: str, password: str) -> Database:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(DSN)
    host = quote(params.get("host") or "localhost", safe="")
    port = params.get("port") or "5432"
    dsn = f"postgresql://{name}:{password}@{host}:{port}/{name}"
    return Database(name, password, dsn, make_conninfo(DSN, dbname=name))


def create_database() -> Database:
    """An empty database, owned by a role of its own."""
    from psycopg import sql

    with admin() as conn:
        name, password = _new_owner(conn)
        conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(name), sql.Identifier(name)))
    return _database(name, password)


def make_template(db: Database) -> None:
    """Make ``db``, which its role has migrated, a template for ``clone_database``: wait for its last connection to
    end (``CREATE DATABASE ... TEMPLATE`` refuses a database anyone is connected to) and hand the database itself to
    the superuser. Its tables stay its role's; were the database its role's too, the ``REASSIGN OWNED`` of a clone,
    which also reaches the databases a role owns, would give this one away to the clone's role."""
    from psycopg import sql

    if left := wait_no_backends(db.name):
        pytest.fail(f"{left} connection(s) to the template {db.name} still open after migrating it")
    with admin() as conn:
        conn.execute(sql.SQL("ALTER DATABASE {} OWNER TO CURRENT_USER").format(sql.Identifier(db.name)))


def clone_database(template: Database) -> Database:
    """A database of its own as ``template`` is, for a role of its own that owns everything in it: a copy of a
    migrated database costs a fraction of migrating an empty one. Dropped with ``drop_database`` as any other."""
    from psycopg import sql

    with admin() as conn:
        name, password = _new_owner(conn)
        conn.execute(
            sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE {}").format(
                sql.Identifier(name), sql.Identifier(name), sql.Identifier(template.name)
            )
        )
    db = _database(name, password)
    # The copy's tables, sequences, types and functions are still the template's role's: give them to the clone's.
    with admin(db.admin_dsn) as conn:
        conn.execute(sql.SQL("REASSIGN OWNED BY {} TO {}").format(sql.Identifier(template.name), sql.Identifier(name)))
    return db


def drop_database(db: Database) -> None:
    from psycopg import sql

    with admin() as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(db.name)))
        # the copy set_reachable keeps while the database is away, left by a test that failed before bringing it back
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(db.name + AWAY)))
        conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(db.name)))


def backends(name: str) -> int:
    from sqlalchemy import func, select

    activity = _activity()
    with admin_engine().connect() as conn:
        return conn.execute(select(func.count()).select_from(activity).where(activity.c.datname == name)).scalar_one()


def wait_no_backends(name: str, timeout: float = 5.0) -> int:
    """Connections still open to the database after ``timeout``; a backend exits shortly after its client."""
    deadline = time.monotonic() + timeout
    while (left := backends(name)) and time.monotonic() < deadline:
        time.sleep(0.1)
    return left


def _end_backends(name: str) -> None:
    """End every connection to the database ``name``."""
    from sqlalchemy import func, select

    activity = _activity()
    with admin_engine().connect() as conn:
        conn.execute(
            select(func.pg_terminate_backend(activity.c.pid)).where(
                activity.c.datname == name, activity.c.pid != func.pg_backend_pid()
            )
        )


def set_reachable(db: Database, reachable: bool) -> None:
    """Stop the database for its clients (refuse new connections, end the open ones), or bring it back with its
    data: Postgres going away, without stopping a server other tests share.

    Away, the database waits as a copy nobody may connect to, and its own name does not exist, so a client trying
    to connect is refused; back, it is created again from that copy, for the same owner. Copying needs the database
    to itself: while ``CREATE DATABASE ... TEMPLATE`` runs, a thread ends every connection to it, the ones open
    before and any a client opened again before the copy took its lock, which new connections then wait for. A
    write a client commits between the end of the copy and the drop is not in the copy."""
    from psycopg import sql

    name, away = sql.Identifier(db.name), sql.Identifier(db.name + AWAY)
    with admin() as conn:
        if reachable:
            conn.execute(sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE {}").format(name, name, away))
            conn.execute(sql.SQL("DROP DATABASE {}").format(away))
            return
        copied = threading.Event()

        def end_until_copied() -> None:
            while not copied.is_set():
                _end_backends(db.name)
                copied.wait(END_EVERY)

        ender = threading.Thread(target=end_until_copied, name=f"end-{db.name}", daemon=True)
        ender.start()
        try:
            conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {} ALLOW_CONNECTIONS false").format(away, name))
        finally:
            copied.set()
            ender.join()
        conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(name))


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
