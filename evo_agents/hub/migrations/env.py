"""Alembic environment of the hub schema.

Run only through ``evo_agents.hub.migrate``, which passes a connection already inside a transaction and
holding the migration lock; the ``alembic`` command line and offline (``--sql``) mode are not supported.
Revisions are hand-written SQL in ``versions/``, named ``NNNN_name.py`` with ``revision = "NNNN"``.
"""

from alembic import context

config = context.config
connection = config.attributes.get("connection")
if connection is None or context.is_offline_mode():
    raise RuntimeError("hub migrations run through `evo-agents hub migrate`, not the alembic command")

applied = config.attributes.get("applied")


def record(ctx, step, heads, run_args) -> None:
    if applied is not None and step.up_revision_id:
        applied.append(step.up_revision_id)


context.configure(connection=connection, target_metadata=None, on_version_apply=record)
with context.begin_transaction():
    context.run_migrations()
