"""The records of an isolated plugin over HTTP (``docs/plugins.md``, section Records).

Two doors onto the one store (``evo_agents.hub.plugin_records``):

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

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import plugin_calls, plugin_records
from evo_agents.hub.plugin_records import COLLECTION, KEY, MAX_PAGE, RecordError
from evo_agents.hub.server import plugin_delegation as delegation
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plugin_delegation import Resolved, may_write
from evo_agents.hub.server.plugin_routes import PluginName, enabled_for
from evo_agents.hub.server.security import CurrentUser
from evo_agents.hub.server.task_dispatch import plugin_of_request

Collection = Annotated[str, Path(pattern=COLLECTION)]
Key = Annotated[str, Path(pattern=KEY)]
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 413, 422)}

backend = APIRouter(
    prefix="/v1/plugin-api/projects/{project}/records",
    tags=["plugin api"],
    responses={401: {"model": ErrorBody}, **REFUSALS},
)
members = APIRouter(prefix="/v1/projects/{project}/plugins/{plugin}/records", tags=["plugins"], responses=REFUSALS)


class RecordIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: dict[str, Any] = Field(description=f"a JSON object, at most {plugin_records.MAX_BODY_BYTES // 1024} KiB")
    level: str | None = Field(None, description="a level of the project's ladder; default: its default label's")
    audience: Literal["writer", "everyone"] | None = Field(
        None, description="writer: only the person who writes it sees it; default: everyone the level reaches"
    )
    revision: int | None = Field(None, ge=0, description="0: a new record; n: the record at revision n; none: either")


class RecordOut(BaseModel):
    collection: str
    key: str
    revision: int
    level: str
    audience: Literal["writer", "everyone"]
    body: dict[str, Any]
    created_by: str | None = Field(description="the login of the writer; none for a job or an event")
    created_at: datetime
    updated_by: str | None
    updated_at: datetime


class RecordPage(BaseModel):
    records: list[RecordOut]
    next_after: str | None = Field(description="the key to pass as `after` for the next page; none at the end")


def _fail(exc: RecordError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


async def _direct(conn: AsyncConnection, user, plugin: str, project: str) -> Resolved:
    """What a member (or a page's bridge, or a run's agent) reaches of ``plugin``'s records with their own grant."""
    access, row = await enabled_for(conn, user, plugin, project)
    if delegation.PERMISSION not in plugin_calls.effective_grant(row):
        raise HTTPException(404, f"plugin {plugin} is not granted {delegation.PERMISSION} in project {project}")
    scope = user.scope
    people = await delegation._people(
        conn, user.user_id, scope.run_id if scope else None, scope.kind if scope else None
    )
    reach = plugin_records.Reach(
        row.project_id, row.id, access.rules, access.max_level, user.user_id, delegation.through(user, None), people
    )
    return Resolved(reach, access.role)


# Both doors run the same operations, in one transaction each.


async def _list(request: Request, resolved_of, collection: str, after: str | None, limit: int) -> RecordPage:
    try:
        async with request.app.state.engine.begin() as conn:
            resolved = await resolved_of(conn)
            records, more = await plugin_records.listing(conn, resolved.reach, collection, after=after, limit=limit)
    except RecordError as exc:
        raise _fail(exc) from None
    return RecordPage(records=records, next_after=more)


async def _show(request: Request, resolved_of, collection: str, key: str) -> RecordOut:
    try:
        async with request.app.state.engine.begin() as conn:
            resolved = await resolved_of(conn)
            return RecordOut(**await plugin_records.get(conn, resolved.reach, collection, key))
    except RecordError as exc:
        raise _fail(exc) from None


async def _write(
    request: Request, response: Response, resolved_of, collection: str, key: str, given: RecordIn
) -> RecordOut:
    try:
        async with request.app.state.engine.begin() as conn:
            resolved = await resolved_of(conn)
            may_write(resolved)
            record, new = await plugin_records.put(
                conn,
                resolved.reach,
                collection,
                key,
                given.body,
                level=given.level,
                audience=given.audience,
                revision=given.revision,
            )
    except RecordError as exc:
        raise _fail(exc) from None
    response.status_code = 201 if new else 200
    return RecordOut(**record)


async def _erase(request: Request, resolved_of, collection: str, key: str, revision: int | None) -> Response:
    try:
        async with request.app.state.engine.begin() as conn:
            resolved = await resolved_of(conn)
            may_write(resolved)
            await plugin_records.remove(conn, resolved.reach, collection, key, revision=revision)
    except RecordError as exc:
        raise _fail(exc) from None
    return Response(status_code=204)


def _plugin_door(request: Request, project: str):
    async def resolved_of(conn: AsyncConnection) -> Resolved:
        plugin_id = await plugin_of_request(request, conn)
        return await delegation.on_behalf(conn, request, plugin_id, project)

    return resolved_of


def _member_door(user, plugin: str, project: str):
    async def resolved_of(conn: AsyncConnection) -> Resolved:
        return await _direct(conn, user, plugin, project)

    return resolved_of


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
