"""The plugin's own tables (API 1.3): a MetaData that holds the hub's keys, the tables a plugin's chain of migrations
adopts from the hub's older chain, and the names of its tables as its manifest's metadata holds them. A plugin writes
its own tables with SQLAlchemy Core and reads the hub's through the API. Private to ``core_plugin_api``."""

from __future__ import annotations

from evo_agents.hub.plugin_calls import core_registry
from evo_agents.hub.plugin_schema import (
    HUB_KEYS,
    PluginChain,
    PluginSchemaError,
    adopt_tables,
    load_metadata,
    own_names,
    plugin_metadata,
)
from evo_agents.hub.server.core_plugin_api._types import Plugin, own_plugin


def own_tables(plugin: Plugin) -> tuple[str, ...]:
    """The names of the tables of the plugin's metadata (its manifest's ``schema.metadata``), the hub's keys left out;
    empty for a plugin without a schema. PluginSchemaError when the metadata breaks a rule of ``plugin_metadata``."""
    own_plugin(plugin)
    for found in core_registry().plugins:
        schema = found.manifest.plugin_schema
        if found.manifest.id == plugin.id and schema is not None:
            chain = PluginChain(plugin.id, schema["metadata"], schema["migrations"], tuple(schema.get("adopts") or ()))
            return tuple(own_names(load_metadata(chain)))
    return ()


__all__ = ["HUB_KEYS", "PluginSchemaError", "adopt_tables", "own_tables", "plugin_metadata"]
