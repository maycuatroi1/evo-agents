"""Alembic environment of the hub schema.

Run only through ``evo_agents.hub.migrate``, which passes a connection already inside a transaction and
holding the migration lock; the ``alembic`` command line and offline (``--sql``) mode are not supported.
Revisions live in ``versions/``, named ``NNNN_name.py`` with ``revision = "NNNN"``; 0001 to 0011 are hand-written
SQL. Autogenerate compares the database with ``evo_agents.hub.tables`` (types and server defaults included) and
leaves procrastinate's tables to procrastinate, and the tables of core plugins to their own chains: those the hub's
older revisions made (``owned.PLUGIN_OWNED_TABLES``) and those of the installed core plugins' own metadata
(``evo_agents.hub.plugin_schema``), with their version tables, ``alembic_version_<id>``.
"""

from alembic import context

from evo_agents.hub.migrations.owned import PLUGIN_OWNED_TABLES
from evo_agents.hub.plugin_schema import installed_tables
from evo_agents.hub.tables import metadata

config = context.config
connection = config.attributes.get("connection")
if connection is None or context.is_offline_mode():
    raise RuntimeError("hub migrations run through `evo-agents hub migrate`, not the alembic command")

applied = config.attributes.get("applied")


def record(ctx, step, heads, run_args) -> None:
    if applied is not None and step.up_revision_id:
        applied.append(step.up_revision_id)


target_metadata = metadata


PLUGIN_TABLES = PLUGIN_OWNED_TABLES | installed_tables()


def _plugins(name: str | None) -> bool:
    return name is not None and (name in PLUGIN_TABLES or name.startswith("alembic_version_"))


def include_name(name, type_, parent_names) -> bool:
    """procrastinate's tables are its own (migration 0004 installs its schema): not in the metadata, not compared;
    nor are the tables of core plugins, which their own chains migrate."""
    if type_ == "table":
        return not (name.startswith("procrastinate_") or _plugins(name))
    return not _plugins(parent_names.get("table_name"))


def include_object(item, name, type_, reflected, compare_to) -> bool:
    if type_ == "table":
        return not _plugins(name)
    table = getattr(item, "table", None)
    return table is None or not _plugins(table.name)


context.configure(
    connection=connection,
    target_metadata=target_metadata,
    include_name=include_name,
    include_object=include_object,
    compare_type=True,
    compare_server_default=True,
    on_version_apply=record,
    # tests that move a database along many revisions commit each one on its own, on a connection outside a
    # transaction: one transaction holding the whole chain takes thousands of locks
    transaction_per_migration=bool(config.attributes.get("transaction_per_migration")),
)
with context.begin_transaction():
    context.run_migrations()
