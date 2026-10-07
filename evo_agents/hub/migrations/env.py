"""Alembic environment of the hub schema.

Run only through ``evo_agents.hub.migrate``, which passes a connection already inside a transaction and
holding the migration lock; the ``alembic`` command line and offline (``--sql``) mode are not supported.
Revisions live in ``versions/``, named ``NNNN_name.py`` with ``revision = "NNNN"``; 0001 to 0011 are hand-written
SQL. Autogenerate compares the database with ``evo_agents.hub.tables`` (types and server defaults included) and
leaves procrastinate's tables to procrastinate.
"""

from alembic import context

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


def include_name(name, type_, parent_names) -> bool:
    """procrastinate's tables are its own (migration 0004 installs its schema): not in the metadata, not compared."""
    return not (type_ == "table" and name.startswith("procrastinate_"))


context.configure(
    connection=connection,
    target_metadata=target_metadata,
    include_name=include_name,
    compare_type=True,
    compare_server_default=True,
    on_version_apply=record,
)
with context.begin_transaction():
    context.run_migrations()
