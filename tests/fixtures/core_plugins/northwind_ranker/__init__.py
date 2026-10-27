"""northwind-ranker, a sample core plugin that contributes a ranker (extension point rankers, version 1): it ranks the
hits of a search it is handed in the way MODE says, and keeps what it was handed in CALLS, for a test to read."""

import asyncio

from evo_agents.hub.server import core_plugin_api as hub

API = (1, 7)
SINK = "ranker@northwind"
CALLS: list[dict] = []
MODE = "reverse"  # reverse, late, boom, junk or sync

MANIFEST = {
    "id": "northwind-ranker",
    "version": "1.0.0",
    "publisher": "northwind",
    "tier": "core",
    "name": "Northwind Ranker",
    "description": "Puts the hits of a search in the order a model service gives them.",
    "engines": {"evo-agents": ">=0.23.1"},
    "plugin_api": 1,
    "permissions": [],
    "egress": [{"id": SINK, "kind": "llm-backend", "host": "api.ranker.northwind.example"}],
    "contributes": {"rankers": [{"name": "rank", "python": "northwind_ranker:rank"}]},
}


def manifest() -> dict:
    hub.require(*API)
    return MANIFEST


async def rank(params: dict, context) -> dict:
    CALLS.append(params)
    ids = [hit["id"] for hit in params["hits"]]
    if MODE == "late":
        await asyncio.sleep(30)
    if MODE == "boom":
        raise RuntimeError("the model service is down")
    if MODE == "junk":
        return {"order": ["no-such-hit"]}
    return {"order": ids[::-1]}
