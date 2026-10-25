"""The tables of core plugins and their chains of migrations (root key ``schema`` of a core plugin's manifest).

A core plugin outside evo-agents carries its own tables, a ``sqlalchemy.MetaData`` of its package (``metadata``), and
its own chain of Alembic revisions (``migrations``, a package of ``NNNN_<name>.py`` files in one straight chain, each
revision id starting with the plugin's table prefix). ``evo_agents.hub.migrate.migrate`` runs the hub's chain first,
then the chain of each installed core plugin, by id, under the same advisory lock, one transaction each; a chain records
its revisions in a version table of its own, ``alembic_version_<id>`` (``-`` made ``_``). The environment of every
plugin chain is the hub's (``evo_agents/hub/plugin_migrations``): the plugin ships revisions, not an env.py.

Every table of a plugin's metadata starts with its table prefix and points its foreign keys only at the hub's keys
(``HUB_KEYS``), which ``plugin_metadata`` holds as stubs; the hub never points a key at a plugin's table, and its own
chain never compares a plugin's table (``evo_agents/hub/migrations/env.py``). ``adopt_tables`` takes tables that exist
already, made by the hub's older chain, as they are: it creates the missing ones, compares the others with the metadata
and stops on a difference, and changes no row.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from sqlalchemy import BigInteger, Column, MetaData, Table, func, select
from sqlalchemy.engine import Connection

from evo_agents.plugins.manifest import table_prefix

log = logging.getLogger(__name__)

HUB_KEYS = (
    "projects.id",
    "users.id",
    "canvas_claims.id",
    "runs.id",
    "invites.id",
)  # what a plugin's foreign key may point at
VERSION_TABLE = "alembic_version_{prefix}"


class PluginSchemaError(RuntimeError):
    """A core plugin's tables or chain that the hub does not run: what is wrong, with the plugin's id."""


@dataclass(frozen=True)
class PluginChain:
    """The chain of migrations of an installed core plugin, as its manifest names it."""

    plugin_id: str
    metadata: str  # module:attribute
    migrations: str  # package of the revisions
    adopts: tuple[str, ...]

    @property
    def version_table(self) -> str:
        return VERSION_TABLE.format(prefix=table_prefix(self.plugin_id).rstrip("_"))

    @property
    def revision_prefix(self) -> str:
        return table_prefix(self.plugin_id)


def plugin_metadata() -> MetaData:
    """A MetaData for a plugin's tables, holding the hub's keys (``HUB_KEYS``) as stub tables so the plugin's foreign
    keys resolve; the stubs are never created, compared or migrated."""
    metadata = MetaData(info={"hub_keys": True})
    for key in HUB_KEYS:
        name, _, column = key.partition(".")
        Table(name, metadata, Column(column, BigInteger, primary_key=True), info={"hub_stub": True})
    return metadata


def own_names(metadata: MetaData) -> list[str]:
    """The tables of ``metadata`` that are the plugin's own: all but the hub's stubs."""
    return sorted(name for name, table in metadata.tables.items() if not table.info.get("hub_stub"))


def check_metadata(plugin_id: str, metadata: MetaData) -> list[str]:
    """What is wrong with the metadata of plugin ``plugin_id``: a table without its prefix, a key that points at a
    table of the hub that HUB_KEYS does not name, or at a stub table that is not the hub's."""
    prefix = table_prefix(plugin_id)
    found = []
    for name in own_names(metadata):
        if not name.startswith(prefix):
            found.append(f"table {name!r} must start with {prefix}, the plugin's prefix")
        for key in metadata.tables[name].foreign_keys:
            target = key.target_fullname
            if not target.startswith(prefix) and target not in HUB_KEYS:
                found.append(
                    f"{name}.{key.parent.name} points at {target}: a plugin's keys point at {', '.join(HUB_KEYS)}"
                )
    for name, table in metadata.tables.items():
        if table.info.get("hub_stub") and f"{name}.{next(iter(table.primary_key.columns)).name}" not in HUB_KEYS:
            found.append(f"stub table {name!r} is not one of the hub's keys")
    return found


def load_metadata(chain: PluginChain) -> MetaData:
    """The plugin's MetaData its manifest names, checked; PluginSchemaError otherwise."""
    module, _, attribute = chain.metadata.partition(":")
    try:
        value = importlib.import_module(module)
        for part in attribute.split("."):
            value = getattr(value, part)
    except Exception as exc:  # the plugin's module may raise anything while it loads
        raise PluginSchemaError(
            f"{chain.plugin_id}: {chain.metadata} does not load: {type(exc).__name__}: {exc}"
        ) from None
    if not isinstance(value, MetaData):
        raise PluginSchemaError(f"{chain.plugin_id}: {chain.metadata} is {type(value).__name__}, not a MetaData")
    problems = check_metadata(chain.plugin_id, value)
    missing = sorted(set(chain.adopts) - set(own_names(value)))
    if missing:
        problems.append(f"schema.adopts names {', '.join(missing)}, which the metadata lacks")
    if problems:
        raise PluginSchemaError(f"{chain.plugin_id}: " + "; ".join(problems))
    return value


def versions_directory(chain: PluginChain) -> Path:
    """The directory of the plugin's revisions."""
    try:
        return Path(str(resources.files(chain.migrations)))
    except Exception as exc:  # ModuleNotFoundError, or a namespace without files
        raise PluginSchemaError(
            f"{chain.plugin_id}: no package {chain.migrations}: {type(exc).__name__}: {exc}"
        ) from None


def chains(plugins=None) -> list[PluginChain]:
    """The chains of the core plugins installed beside evo-agents (``evo_agents.plugins.core``), by id."""
    if plugins is None:
        from evo_agents.plugins.core import core_plugins

        plugins = core_plugins()
    found = []
    for plugin in plugins:
        schema = plugin.manifest.plugin_schema
        if schema is not None:
            found.append(
                PluginChain(
                    plugin.manifest.id, schema["metadata"], schema["migrations"], tuple(schema.get("adopts") or ())
                )
            )
    return sorted(found, key=lambda chain: chain.plugin_id)


def installed_tables(plugins=None) -> frozenset[str]:
    """The names of the tables the installed core plugins carry in their own metadata; a plugin whose metadata does
    not load is logged and adds none."""
    names: set[str] = set()
    for chain in chains(plugins):
        try:
            names.update(own_names(load_metadata(chain)))
        except PluginSchemaError as exc:
            log.warning("core plugin tables not read", extra={"plugin": chain.plugin_id, "error": str(exc)})
    return frozenset(names)


# Adopting tables


def _exists(connection: Connection, name: str) -> bool:
    return connection.execute(select(func.to_regclass(name))).scalar() is not None


def adopt_tables(connection: Connection, metadata: MetaData, names) -> list[str]:
    """Take tables ``names`` of ``metadata`` as the plugin's: create each one that does not exist, then compare those
    that did with the metadata (Alembic's compare_metadata, types and server defaults included) and raise
    PluginSchemaError naming each difference. No row is written, changed or deleted. The names created, in order."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    wanted = list(names)
    unknown = sorted(set(wanted) - set(own_names(metadata)))
    if unknown:
        raise PluginSchemaError(f"adopt_tables: {', '.join(unknown)} not in the plugin's metadata")
    created, kept = [], set()
    for name in wanted:
        if _exists(connection, name):
            kept.add(name)
        else:
            metadata.tables[name].create(connection)
            created.append(name)
    if kept:

        def include_name(name, type_, parent_names) -> bool:
            if type_ == "table":
                return name in kept
            return parent_names.get("table_name") in kept if parent_names.get("table_name") else True

        def include_object(item, name, type_, reflected, compare_to) -> bool:
            if type_ == "table":
                return name in kept
            table = getattr(item, "table", None)
            return table is None or table.name in kept

        context = MigrationContext.configure(
            connection,
            opts={
                "compare_type": True,
                "compare_server_default": True,
                "include_name": include_name,
                "include_object": include_object,
            },
        )
        diff = compare_metadata(context, metadata)
        if diff:
            raise PluginSchemaError(
                "the tables to adopt differ from the plugin's metadata: " + "; ".join(map(_said, diff))
            )
    return created


def _said(item) -> str:
    if isinstance(item, list):  # modify_* come grouped
        return ", ".join(_said(part) for part in item)
    kind = item[0]
    if kind in ("add_column", "remove_column"):
        return f"{kind} {item[2]}.{item[3].name}"
    if kind.startswith("modify_"):
        return f"{kind} {item[2]}.{item[3]}"
    target = item[1]
    return f"{kind} {getattr(target, 'name', target)}"
