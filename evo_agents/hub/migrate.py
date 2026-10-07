"""Alembic migrations of the hub database, applied under a Postgres advisory lock.

The api and the worker both migrate when they start, and ``evo-agents hub migrate`` does the same on its
own. Whoever runs first takes ``pg_advisory_lock`` on a connection of its own and applies every pending
revision in one transaction; the others wait on the lock, then find the schema at head and change nothing.
A database at a revision this package does not know (written by a newer release) is refused before
anything runs: a hub never writes to a schema it cannot read.

Alembic is driven from code against the scripts shipped in ``evo_agents/hub/migrations``; there is no
alembic.ini. Migrating runs on an engine of its own over psycopg 3 (``postgresql+psycopg``), apart from the
server's pool and the engine on it (``evo_agents.hub.db``).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import BigInteger, column, create_engine, func, literal, select, table
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from evo_agents import __version__

log = logging.getLogger(__name__)

LOCK_KEY = 0x65766F2D687562  # "evo-hub" in ASCII; advisory locks are per database
LOCK_TIMEOUT = 300.0  # seconds to wait for another process's migration before giving up
CONNECT_TIMEOUT = 10
APPLICATION_NAME = "evo-agents-hub-migrate"


class MigrationError(RuntimeError):
    """The database cannot be brought to this package's schema; nothing was changed."""


@dataclass(frozen=True)
class MigrationResult:
    before: tuple[str, ...]  # revisions recorded in the database before, empty for a new database
    after: tuple[str, ...]
    applied: tuple[str, ...]  # in the order they ran; empty when the schema was already at head


def script_location() -> Path:
    """The migration scripts inside the installed package."""
    return Path(str(resources.files("evo_agents.hub") / "migrations"))


def alembic_config() -> Config:
    config = Config()  # no ini file: everything Alembic needs is set here
    config.set_main_option("script_location", str(script_location()).replace("%", "%%"))
    return config


def head_revision() -> str:
    heads = ScriptDirectory.from_config(alembic_config()).get_heads()
    if len(heads) != 1:
        raise MigrationError(f"the hub migrations must form one chain, found heads {sorted(heads)}")
    return heads[0]


def revisions() -> tuple[str, ...]:
    """Every revision of the hub migrations, oldest first; the last one is ``head_revision()``."""
    script = ScriptDirectory.from_config(alembic_config())
    return tuple(reversed([revision.revision for revision in script.walk_revisions(head=head_revision())]))


def _engine(dsn: str):
    # The URL only picks the dialect; connections come from libpq with the DSN as given, in either form.
    def connect():
        return psycopg.connect(dsn, connect_timeout=CONNECT_TIMEOUT, application_name=APPLICATION_NAME)

    return create_engine("postgresql+psycopg://", creator=connect, poolclass=NullPool)


# Alembic's record of the revisions applied; the table is Alembic's, not the hub's, so it is not in tables.py.
ALEMBIC_VERSION = table("alembic_version", column("version_num"))


def _key():
    return literal(LOCK_KEY, BigInteger)  # beyond integer: bound as bigint


def _lock(conn: Connection, timeout: float) -> None:
    if conn.execute(select(func.pg_try_advisory_lock(_key()))).scalar():
        conn.commit()
        return
    log.info("waiting for the migration lock held by another hub process", extra={"timeout_s": timeout})
    started = time.monotonic()
    # lock_timeout holds for this transaction only: the commit once the lock is taken resets it, and the lock, taken
    # for the session, stays.
    conn.execute(select(func.set_config("lock_timeout", f"{int(timeout * 1000)}ms", True)))
    try:
        conn.execute(select(func.pg_advisory_lock(_key())))
    except OperationalError as exc:
        if isinstance(exc.orig, psycopg.errors.LockNotAvailable):
            raise MigrationError(f"another process held the migration lock for more than {timeout:.0f}s") from None
        raise
    conn.commit()
    log.info("migration lock acquired", extra={"waited_s": round(time.monotonic() - started, 3)})


def _recorded(conn: Connection) -> tuple[str, ...]:
    """The revisions in alembic_version, empty before the first migration."""
    if conn.execute(select(func.to_regclass("alembic_version"))).scalar() is None:
        return ()
    version = ALEMBIC_VERSION.c.version_num
    return tuple(conn.execute(select(version).order_by(version)).scalars())


def _unlock(conn: Connection) -> None:
    try:
        conn.execute(select(func.pg_advisory_unlock(_key())))
        conn.commit()
    except Exception as exc:  # closing the connection releases the lock anyway
        log.warning("could not release the migration lock: %s", type(exc).__name__)


def migrate(dsn: str, *, lock_timeout: float = LOCK_TIMEOUT) -> MigrationResult:
    """Bring the database at ``dsn`` to head. Safe to run from several processes at once."""
    config = alembic_config()
    script = ScriptDirectory.from_config(config)
    head = head_revision()
    known = {revision.revision for revision in script.walk_revisions()}
    engine = _engine(dsn)
    try:
        with engine.connect() as conn:
            _lock(conn, lock_timeout)
            try:
                with conn.begin():  # every pending revision commits together or not at all
                    before = _recorded(conn)
                    unknown = sorted(set(before) - known)
                    if unknown:
                        raise MigrationError(
                            f"the database is at revision {', '.join(unknown)}, which evo-agents {__version__} does "
                            f"not know (its newest is {head}): run the release that wrote it, or a later one"
                        )
                    applied: list[str] = []
                    config.attributes["connection"] = conn
                    config.attributes["applied"] = applied
                    command.upgrade(config, "head")
                    after = _recorded(conn)
            finally:
                _unlock(conn)
    finally:
        engine.dispose()
    result = MigrationResult(before, after, tuple(applied))
    if result.applied:
        log.info("migrations applied", extra={"applied": list(result.applied), "schema": head})
    else:
        log.info("schema already at head", extra={"schema": head})
    return result
