"""Alembic migrations of the hub database, applied under a Postgres advisory lock.

The api and the worker both migrate when they start, and ``evo-agents hub migrate`` does the same on its
own. Whoever runs first takes ``pg_advisory_lock`` on a connection of its own and applies every pending
revision in one transaction; the others wait on the lock, then find the schema at head and change nothing.
A database at a revision this package does not know (written by a newer release) is refused before
anything runs: a hub never writes to a schema it cannot read.

After the hub's chain, still under the lock, the chain of each installed core plugin that carries tables of its
own runs, by plugin id, in a transaction of its own and with a version table of its own (``alembic_version_<id>``;
``evo_agents.hub.plugin_schema``). A plugin whose chain does not load, does not apply or was written further by a newer
release of the plugin is logged and left as it was: the hub's chain and the other plugins' still run.

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
from evo_agents.hub import plugin_schema
from evo_agents.hub.plugin_schema import PluginChain, PluginSchemaError

log = logging.getLogger(__name__)

LOCK_KEY = 0x65766F2D687562  # "evo-hub" in ASCII; advisory locks are per database
LOCK_TIMEOUT = 300.0  # seconds to wait for another process's migration before giving up
CONNECT_TIMEOUT = 10
APPLICATION_NAME = "evo-agents-hub-migrate"


class MigrationError(RuntimeError):
    """The database cannot be brought to this package's schema; nothing was changed."""


@dataclass(frozen=True)
class PluginMigration:
    """What migrating did to the chain of one core plugin; ``error`` says why it was left as it was."""

    plugin_id: str
    before: tuple[str, ...]
    after: tuple[str, ...]
    applied: tuple[str, ...]
    error: str | None = None


@dataclass(frozen=True)
class MigrationResult:
    before: tuple[str, ...]  # revisions recorded in the database before, empty for a new database
    after: tuple[str, ...]
    applied: tuple[str, ...]  # in the order they ran; empty when the schema was already at head
    plugins: tuple[PluginMigration, ...] = ()  # the chains of the core plugins, by id


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


def plugin_script_location() -> Path:
    """The environment every core plugin's chain runs in."""
    return Path(str(resources.files("evo_agents.hub") / "plugin_migrations"))


def plugin_alembic_config(chain: PluginChain, metadata) -> Config:
    config = Config()
    config.set_main_option("script_location", str(plugin_script_location()).replace("%", "%%"))
    config.set_main_option("version_locations", str(plugin_schema.versions_directory(chain)).replace("%", "%%"))
    config.attributes["chain"] = chain
    config.attributes["metadata"] = metadata
    return config


def plugin_script(chain: PluginChain, config: Config) -> tuple[ScriptDirectory, str]:
    """The revisions of a plugin's chain and its head; PluginSchemaError unless they form one chain of revisions
    named with the plugin's prefix."""
    script = ScriptDirectory.from_config(config)
    try:
        heads = script.get_heads()
        known = [revision.revision for revision in script.walk_revisions()]
    except Exception as exc:  # a revision that does not import, or a cycle
        raise PluginSchemaError(f"{chain.plugin_id}: its revisions do not read: {type(exc).__name__}: {exc}") from None
    if len(heads) != 1:
        raise PluginSchemaError(f"{chain.plugin_id}: its migrations must form one chain, found heads {sorted(heads)}")
    wrong = sorted(name for name in known if not name.startswith(chain.revision_prefix))
    if wrong:
        raise PluginSchemaError(
            f"{chain.plugin_id}: revisions {', '.join(wrong)} must start with {chain.revision_prefix}"
        )
    return script, heads[0]


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


def _recorded(conn: Connection, name: str = "alembic_version") -> tuple[str, ...]:
    """The revisions in version table ``name`` (the hub's, alembic_version), empty before the first migration."""
    if conn.execute(select(func.to_regclass(name))).scalar() is None:
        return ()
    version = (
        ALEMBIC_VERSION.c.version_num if name == "alembic_version" else table(name, column("version_num")).c.version_num
    )
    return tuple(conn.execute(select(version).order_by(version)).scalars())


def _plugin_chain(conn: Connection, chain: PluginChain) -> PluginMigration:
    """Bring the chain of one core plugin to its head, in a transaction of its own; PluginSchemaError, and nothing
    changed, when it cannot."""
    metadata = plugin_schema.load_metadata(chain)
    config = plugin_alembic_config(chain, metadata)
    script, head = plugin_script(chain, config)
    known = {revision.revision for revision in script.walk_revisions()}
    with conn.begin():
        before = _recorded(conn, chain.version_table)
        unknown = sorted(set(before) - known)
        if unknown:
            raise PluginSchemaError(
                f"{chain.plugin_id}: the database is at revision {', '.join(unknown)}, which the installed release of "
                f"the plugin does not know (its newest is {head})"
            )
        applied: list[str] = []
        config.attributes["connection"] = conn
        config.attributes["applied"] = applied
        command.upgrade(config, "head")
        after = _recorded(conn, chain.version_table)
    return PluginMigration(chain.plugin_id, before, after, tuple(applied))


def _plugin_chains(conn: Connection, chains: list[PluginChain], *, strict: bool) -> tuple[PluginMigration, ...]:
    done = []
    for chain in chains:
        try:
            done.append(_plugin_chain(conn, chain))
        except Exception as exc:  # the plugin's revisions may raise anything; nothing of it committed
            if strict:
                raise MigrationError(f"core plugin {chain.plugin_id}: {type(exc).__name__}: {exc}") from exc
            log.warning(
                "core plugin migrations not applied",
                extra={"plugin": chain.plugin_id, "error": f"{type(exc).__name__}: {exc}"},
            )
            done.append(PluginMigration(chain.plugin_id, (), (), (), f"{type(exc).__name__}: {exc}"))
            continue
        if done[-1].applied:
            log.info(
                "core plugin migrations applied", extra={"plugin": chain.plugin_id, "applied": list(done[-1].applied)}
            )
    return tuple(done)


def _unlock(conn: Connection) -> None:
    try:
        conn.execute(select(func.pg_advisory_unlock(_key())))
        conn.commit()
    except Exception as exc:  # closing the connection releases the lock anyway
        log.warning("could not release the migration lock: %s", type(exc).__name__)


def migrate(dsn: str, *, lock_timeout: float = LOCK_TIMEOUT, plugins=None, only: str | None = None) -> MigrationResult:
    """Bring the database at ``dsn`` to head, then the chain of each installed core plugin (``plugins``: the core
    plugins to read the chains of, those installed when None). With ``only``, the chain of that plugin alone, on a
    hub already at head, and a failure of it is a MigrationError. Safe to run from several processes at once."""
    chains = plugin_schema.chains(plugins)
    if only is not None:
        chains = [chain for chain in chains if chain.plugin_id == only]
        if not chains:
            raise MigrationError(f"no installed core plugin {only} carries tables of its own")
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
                    if only is None:
                        command.upgrade(config, "head")
                    elif set(before) != {head}:
                        raise MigrationError(f"the hub's schema is not at {head}: run evo-agents hub migrate first")
                    after = _recorded(conn)
                done = _plugin_chains(conn, chains, strict=only is not None)
            finally:
                _unlock(conn)
    finally:
        engine.dispose()
    result = MigrationResult(before, after, tuple(applied), done)
    if result.applied:
        log.info("migrations applied", extra={"applied": list(result.applied), "schema": head})
    else:
        log.info("schema already at head", extra={"schema": head})
    return result


@dataclass(frozen=True)
class ChainStatus:
    """Where one chain stands: the hub's (``chain`` "hub") or a core plugin's."""

    chain: str
    version_table: str
    recorded: tuple[str, ...]
    head: str | None  # None when the plugin's chain does not read
    error: str | None = None


def status(dsn: str, plugins=None) -> list[ChainStatus]:
    """The revision each chain is at and its head, the hub's first, read without the lock."""
    engine = _engine(dsn)
    found = []
    try:
        with engine.connect() as conn:
            found.append(ChainStatus("hub", "alembic_version", _recorded(conn), head_revision()))
            for chain in plugin_schema.chains(plugins):
                recorded = _recorded(conn, chain.version_table)
                try:
                    config = plugin_alembic_config(chain, plugin_schema.load_metadata(chain))
                    head = plugin_script(chain, config)[1]
                except PluginSchemaError as exc:
                    found.append(ChainStatus(chain.plugin_id, chain.version_table, recorded, None, str(exc)))
                    continue
                found.append(ChainStatus(chain.plugin_id, chain.version_table, recorded, head))
    finally:
        engine.dispose()
    return found
