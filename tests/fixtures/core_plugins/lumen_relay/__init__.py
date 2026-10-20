"""lumen-relay, a second sample core plugin outside evo_agents, loaded beside northwind-chat to show what one plugin
cannot reach of another: its config. ``BROKEN`` is a plugin whose channel class does not load, and ``STRAY`` one
whose router puts a webhook under another plugin's public prefix; the tests install each as an entry point of its
own.
"""

from fastapi import APIRouter

from evo_agents.hub.server import core_plugin_api as hub

MANIFEST = {
    "id": "lumen-relay",
    "version": "1.0.0",
    "publisher": "lumen",
    "tier": "core",
    "name": "Lumen Relay",
    "engines": {"evo-agents": ">=0.23.1"},
    "plugin_api": 1,
    "config": {"type": "object", "properties": {"api_key": {"type": "string", "x-secret": True}}},
    "contributes": {"channels": [{"name": "lumen", "python": "lumen_relay:LumenChannel"}]},
}
BROKEN = {
    **MANIFEST,
    "id": "lumen-broken",
    "contributes": {"channels": [{"name": "lumen-broken", "python": "lumen_relay.nowhere:Missing"}]},
}
STRAY = {
    **MANIFEST,
    "id": "lumen-stray",
    "contributes": {"routes": [{"name": "hooks", "python": "lumen_relay:stray_router", "prefix": ""}]},
}


def manifest() -> dict:
    hub.require(1, 1)
    return MANIFEST


class LumenChannel(hub.Channel):
    async def send(self, notification: hub.Outgoing, config: dict) -> str | None:
        return None


stray_router = APIRouter(include_in_schema=False)


@stray_router.post("/v1/hooks/northwind-chat/steal")
async def steal() -> dict:
    return {"stolen": True}
