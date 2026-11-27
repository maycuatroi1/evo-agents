"""The records of an isolated plugin over HTTP (``docs/plugins.md``, section Records).

Two doors onto the one store (``evo_agents.hub.plugin_records``), which run the same operations (``service.records``),
in one transaction each:

- ``/v1/plugin-api/projects/{project}/records/{collection}[/{key}]`` for the plugin's backend, with its plugin token
  (``Authorization: Bearer evp_...``) and the delegation of the call it serves (``X-Evo-On-Behalf-Of``,
  ``plugin_delegation``). The plugin is the token's: no path names one, so no backend reaches another plugin's records.
- ``/v1/projects/{project}/plugins/{plugin}/records/{collection}[/{key}]`` for a member, a page's bridge or a run's
  agent, with their own credential: they reach what their grant, and for an agent its sink, reach. A page's bridge
  reaches only the records of its own plugin (``plugin_bridge``).

GET a collection lists at most ``limit`` records after the key ``after``; GET a key reads one; PUT a key writes it
(201 when new, else 200): ``revision`` 0 asks for a new record, a number for that very revision (409 when the record has
moved on); DELETE a key deletes it (``?revision=`` likewise). A record the call may not see answers 404, as one that
does not exist. Both need ``records:use`` in what the project granted the plugin.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.plugin_records import COLLECTION, KEY, MAX_PAGE
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plugin_api.models.records import RecordIn, RecordOut, RecordPage
from evo_agents.hub.server.plugin_api.service import records
from evo_agents.hub.server.plugin_api.service.records import Door
from evo_agents.hub.server.plugins.models.delegation import Resolved
from evo_agents.hub.server.plugins.models.extension import PluginName
from evo_agents.hub.server.plugins.service import delegation
from evo_agents.hub.server.security import CurrentUser
from evo_agents.hub.server.task_dispatch import plugin_of_request

Collection = Annotated[str, Path(pattern=COLLECTION)]
Key = Annotated[str, Path(pattern=KEY)]
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409, 413, 422)}

backend = APIRouter(
    prefix="/v1/plugin-api/projects/{project}/records",
    tags=["plugin api"],
    responses={401: {"model": ErrorBody}, **REFUSALS},
)
members = APIRouter(prefix="/v1/projects/{project}/plugins/{plugin}/records", tags=["plugins"], responses=REFUSALS)


def _plugin_door(request: Request, project: str) -> Door:
    async def resolved_of(conn: AsyncConnection) -> Resolved:
        plugin_id = await plugin_of_request(request, conn)
        return await delegation.on_behalf(conn, request, plugin_id, project)

    return resolved_of


def _member_door(user, plugin: str, project: str) -> Door:
    async def resolved_of(conn: AsyncConnection) -> Resolved:
        return await records.direct(conn, user, plugin, project)

    return resolved_of


async def _list(request: Request, door: Door, collection: str, after: str | None, limit: int) -> RecordPage:
    async with request.app.state.engine.begin() as conn:
        return await records.listing(conn, door, collection, after, limit)


async def _show(request: Request, door: Door, collection: str, key: str) -> RecordOut:
    async with request.app.state.engine.begin() as conn:
        return await records.show(conn, door, collection, key)


async def _write(
    request: Request, response: Response, door: Door, collection: str, key: str, given: RecordIn
) -> RecordOut:
    async with request.app.state.engine.begin() as conn:
        record, new = await records.write(conn, door, collection, key, given)
    response.status_code = 201 if new else 200
    return record


async def _erase(request: Request, door: Door, collection: str, key: str, revision: int | None) -> Response:
    async with request.app.state.engine.begin() as conn:
        await records.erase(conn, door, collection, key, revision)
    return Response(status_code=204)


After = Annotated[str | None, Query(description="list the records after this key")]
Limit = Annotated[int, Query(ge=1, le=MAX_PAGE)]
Expected = Annotated[int | None, Query(ge=1, description="delete only the record at this revision")]


@backend.get("/{collection}", response_model=RecordPage, summary="List a collection's records")
async def backend_list(
    request: Request, project: ProjectName, collection: Collection, after: After = None, limit: Limit = 50
) -> RecordPage:
    return await _list(request, _plugin_door(request, project), collection, after, limit)


@backend.get("/{collection}/{key}", response_model=RecordOut, summary="Read a record")
async def backend_show(request: Request, project: ProjectName, collection: Collection, key: Key) -> RecordOut:
    return await _show(request, _plugin_door(request, project), collection, key)


@backend.put("/{collection}/{key}", response_model=RecordOut, summary="Write a record")
async def backend_put(
    request: Request, response: Response, project: ProjectName, collection: Collection, key: Key, given: RecordIn
) -> RecordOut:
    return await _write(request, response, _plugin_door(request, project), collection, key, given)


@backend.delete("/{collection}/{key}", status_code=204, summary="Delete a record")
async def backend_delete(
    request: Request, project: ProjectName, collection: Collection, key: Key, revision: Expected = None
) -> Response:
    return await _erase(request, _plugin_door(request, project), collection, key, revision)


@members.get("/{collection}", response_model=RecordPage, summary="List a plugin's records of a collection")
async def member_list(
    request: Request,
    user: CurrentUser,
    project: ProjectName,
    plugin: PluginName,
    collection: Collection,
    after: After = None,
    limit: Limit = 50,
) -> RecordPage:
    return await _list(request, _member_door(user, plugin, project), collection, after, limit)


@members.get("/{collection}/{key}", response_model=RecordOut, summary="Read a record of a plugin")
async def member_show(
    request: Request, user: CurrentUser, project: ProjectName, plugin: PluginName, collection: Collection, key: Key
) -> RecordOut:
    return await _show(request, _member_door(user, plugin, project), collection, key)


@members.put("/{collection}/{key}", response_model=RecordOut, summary="Write a record of a plugin")
async def member_put(
    request: Request,
    response: Response,
    user: CurrentUser,
    project: ProjectName,
    plugin: PluginName,
    collection: Collection,
    key: Key,
    given: RecordIn,
) -> RecordOut:
    return await _write(request, response, _member_door(user, plugin, project), collection, key, given)


@members.delete("/{collection}/{key}", status_code=204, summary="Delete a record of a plugin")
async def member_delete(
    request: Request,
    user: CurrentUser,
    project: ProjectName,
    plugin: PluginName,
    collection: Collection,
    key: Key,
    revision: Expected = None,
) -> Response:
    return await _erase(request, _member_door(user, plugin, project), collection, key, revision)
