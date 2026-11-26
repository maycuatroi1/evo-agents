"""The commands plugins add to the CLI (extension point ``cli``, version 1): ``evo-agents hub <plugin> <command>``.

    GET /v1/cli/plugins

answers, for any member signed in, the commands of each plugin installed on the hub and turned on, as their manifests
declare them: a name, help, the plugin's route each one calls (``http``), whether it takes ``--project``, its arguments
and how to print the answer. The CLI keeps the answer (``evo_agents.hub.cli_plugins``) and asks again with
``If-None-Match``: the ETag is the SHA-256 of the answer, so a 304 costs nothing while no plugin changed. A command runs
only as a call of a route, with the member's own token, which the route checks as any other call: the list grants
nothing.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select

from evo_agents.hub import tables
from evo_agents.hub.server.security import CurrentUser

VERSION = 1  # of the answer's shape
PATH = "/v1/cli/plugins"

router = APIRouter(prefix="/v1/cli", tags=["plugins"])


async def plugin_commands(conn) -> dict[str, Any]:
    """The answer of GET /v1/cli/plugins: the commands of the plugins installed and on, by plugin id."""
    p, v = tables.plugins, tables.plugin_versions
    query = (
        select(p.c.name, p.c.installed_version, v.c.manifest)
        .join_from(p, v, (v.c.plugin_id == p.c.id) & (v.c.version == p.c.installed_version))
        .where(p.c.disabled_reason.is_(None))
        .order_by(p.c.name)
    )
    found = []
    for row in (await conn.execute(query)).all():
        commands = ((row.manifest or {}).get("contributes") or {}).get("cli") or []
        if commands:
            found.append({"id": row.name, "version": row.installed_version, "commands": commands})
    return {"version": VERSION, "plugins": found}


def etag_of(body: dict) -> str:
    data = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return '"' + hashlib.sha256(data).hexdigest() + '"'


@router.get(
    "/plugins",
    summary="The commands plugins add to the CLI",
    responses={
        200: {"description": "{version, plugins: [{id, version, commands}]}"},
        304: {"description": "unchanged"},
    },
)
async def cli_plugins(
    request: Request,
    user: CurrentUser,
    if_none_match: Annotated[str | None, Header(description="the ETag of the answer the CLI holds")] = None,
) -> Response:
    async with request.app.state.engine.begin() as conn:
        body = await plugin_commands(conn)
    etag = etag_of(body)
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if if_none_match is not None and etag in {tag.strip() for tag in if_none_match.split(",")}:
        return Response(status_code=304, headers=headers)
    return JSONResponse(body, headers=headers)
