"""Alembic environment of every core plugin's chain of migrations (``evo_agents.hub.plugin_schema``).

Run only through ``evo_agents.hub.migrate``, which passes a connection inside a transaction and holding the
migration lock, the plugin's chain (``chain``) and its metadata (``metadata``): the revisions are the plugin's, in
its ``migrations`` package (Alembic's ``version_locations``), and the record of those applied is its own table,
``alembic_version_<id>``. Autogenerate compares the plugin's own tables only: the hub's tables, and its keys the
plugin's metadata holds as stubs, are not the plugin's to migrate.
"""

from alembic import context

from evo_agents.hub.plugin_schema import own_names

config = context.config
connection = config.attributes.get("connection")
chain = config.attributes.get("chain")
if connection is None or chain is None or context.is_offline_mode():
    raise RuntimeError("core plugin migrations run through `evo-agents hub migrate`, not the alembic command")

applied = config.attributes.get("applied")
metadata = config.attributes["metadata"]
owned = set(own_names(metadata))


def record(ctx, step, heads, run_args) -> None:
    if applied is not None and step.up_revision_id:
        applied.append(step.up_revision_id)


def include_name(name, type_, parent_names) -> bool:
    if type_ == "table":
        return name in owned
    return True


def include_object(item, name, type_, reflected, compare_to) -> bool:
    if type_ == "table":
        return name in owned
    return True


context.configure(
    connection=connection,
    target_metadata=metadata,
    include_name=include_name,
    include_object=include_object,
    compare_type=True,
    compare_server_default=True,
    version_table=chain.version_table,
    on_version_apply=record,
)
with context.begin_transaction():
    context.run_migrations()
