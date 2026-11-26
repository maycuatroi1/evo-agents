"""The routes of the plugin host (``evo_agents.hub.plugin_host_service``), which the api asks through
``evo_agents.hub.plugin_calls.PluginHost``: the host serves them, the api calls them, both write them from here."""

from __future__ import annotations

from typing import Any

from evo_agents.hub.api.routes import V1, route

BACKENDS = route("host/backends")
RELOAD = route("host/reload")
PLUGIN_CALL_ROUTE = f"{V1}/host/plugins/{{name}}/call"  # the host's route, as FastAPI declares it


def plugin_call(name: Any) -> str:
    """``/v1/host/plugins/<name>/call``: one plugin/1 call on the backend of plugin ``name``."""
    return route("host/plugins", name, "call")
