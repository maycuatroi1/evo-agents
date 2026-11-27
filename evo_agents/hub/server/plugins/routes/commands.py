"""The commands plugins add to the CLI (extension point ``cli``, version 1): ``evo-agents hub <plugin> <command>``.

    GET /v1/cli/plugins

answers, for any member signed in, the commands of each plugin installed on the hub and turned on, as their manifests
declare them: a name, help, the plugin's route each one calls (``http``), whether it takes ``--project``, its arguments
and how to print the answer. The CLI keeps the answer (``evo_agents.hub.cli.extensions``) and asks again with
``If-None-Match``: the ETag is the SHA-256 of the answer, so a 304 costs nothing while no plugin changed. A command runs
only as a call of a route, with the member's own token, which the route checks as any other call: the list grants
nothing.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, Response

from evo_agents.hub.server.plugins.service.commands import etag_of, plugin_commands
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1/cli", tags=["plugins"])


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
