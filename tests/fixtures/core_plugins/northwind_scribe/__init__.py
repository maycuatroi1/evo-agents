"""northwind-scribe, the sample core plugin of API 1.6 outside evo_agents: it sends a project's text to a model service,
so its manifest declares the sink it does it through (key ``egress``), and it reads what the project's policy lets that
sink read and writes decisions through ``evo_agents.hub.server.core_plugin_api`` alone."""

from evo_agents.hub.server import core_plugin_api as hub

API = (1, 6)  # the version of the API this plugin is written for
SINK = "scribe@northwind"

MANIFEST = {
    "id": "northwind-scribe",
    "version": "1.0.0",
    "publisher": "northwind",
    "tier": "core",
    "name": "Northwind Scribe",
    "description": "Reads why runs failed and what people corrected, and decides on the graph.",
    "engines": {"evo-agents": ">=0.23.1"},
    "plugin_api": 1,
    "permissions": [],
    "egress": [{"id": SINK, "kind": "llm-backend", "host": "api.scribe.northwind.example"}],
    "contributes": {},
}


def manifest() -> dict:
    """The manifest, once this hub serves the version of the API the plugin is written for."""
    hub.require(*API)
    return MANIFEST
