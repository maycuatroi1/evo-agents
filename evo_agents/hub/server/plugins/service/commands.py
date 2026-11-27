"""The answer of GET /v1/cli/plugins (``routes.commands``): the commands of each plugin installed and on, and its
ETag."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.plugins.models.commands import VERSION
from evo_agents.hub.server.plugins.repository.commands import installed_manifests


async def plugin_commands(engine: AsyncEngine) -> dict[str, Any]:
    """The answer of GET /v1/cli/plugins: the commands of the plugins installed and on, by plugin id."""
    found = []
    async with UnitOfWork(engine) as conn:
        rows = await installed_manifests(conn)
    for row in rows:
        commands = ((row.manifest or {}).get("contributes") or {}).get("cli") or []
        if commands:
            found.append({"id": row.name, "version": row.installed_version, "commands": commands})
    return {"version": VERSION, "plugins": found}


def etag_of(body: dict) -> str:
    data = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return '"' + hashlib.sha256(data).hexdigest() + '"'
