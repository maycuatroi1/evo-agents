"""The routes of /v1/memories: each reads the request, hands it to the service (``service.core``, ``service.upkeep.restore``) and
answers what it returns.

A request is read up to MAX_REQUEST bytes before FastAPI parses it (``BoundedRoute``). A write the service finds
stale (``core.Stale``) is answered with the 409 ``Conflict``, which carries the version the hub holds when the
caller may see it. ``restore_router`` holds POST /v1/memories/{id}/restore, which the app includes with the account
routes, before ``router``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from evo_agents.hub.memory import AGENT_SINK
from evo_agents.hub.profiles import HANDLE_PATTERN
from evo_agents.hub.server.base.models import PROJECT_NAME
from evo_agents.hub.server.errors import CODES, ErrorBody
from evo_agents.hub.server.memories.models import (
    DEFAULT_LIMIT,
    DEFAULT_REVISIONS,
    MAX_ID,
    MAX_LIMIT,
    MAX_REQUEST,
    MAX_REVISION,
    MAX_REVISIONS,
    REFUSALS,
    SEARCH_LIMIT,
    TOO_LARGE,
    AudienceIn,
    Conflict,
    Memory,
    MemoryRevision,
    Page,
    Results,
    Revisions,
    Written,
)
from evo_agents.hub.server.memories.service import core, upkeep
from evo_agents.hub.server.memories.service.inputs import MemoryIn
from evo_agents.hub.server.projects import PRINTABLE
from evo_agents.hub.server.security import CurrentUser

Sink = Annotated[
    str,
    Query(min_length=1, max_length=100, pattern=PRINTABLE, description="the sink reading the memories (read rule)"),
]
ReadSink = Annotated[
    str | None,
    Query(
        min_length=1,
        max_length=100,
        pattern=PRINTABLE,
        description=(
            "the sink reading the memories (read rule); left out, claude-code@anthropic for a machine token and none "
            "for a web session, which reads by its grant alone"
        ),
    ),
]
AgentHandle = Annotated[
    str | None,
    Query(
        pattern=HANDLE_PATTERN,
        description="only the memories of this agent profile (its handle, @ left out); left out, those of no agent",
    ),
]
MemoryId = Annotated[int, Path(ge=1, le=MAX_ID)]
RevisionNumber = Annotated[int, Path(ge=1, le=MAX_REVISION)]


class BoundedRoute(APIRoute):
    """Reads at most MAX_REQUEST bytes of a request before FastAPI parses it: a larger one is a 413, whether it says
    its length or not, and is never held in memory whole."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request: Request):
            declared = request.headers.get("content-length")
            if declared is not None and (not declared.isdigit() or int(declared) > MAX_REQUEST):
                raise HTTPException(413, TOO_LARGE)
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_REQUEST:
                    raise HTTPException(413, TOO_LARGE)
                chunks.append(chunk)
            request._body = b"".join(chunks)  # what Request.body() returns from now on
            return await handler(request)

        return bounded


router = APIRouter(
    prefix="/v1/memories", tags=["memories"], route_class=BoundedRoute, responses={401: {"model": ErrorBody}}
)
restore_router = APIRouter(
    prefix="/v1/memories", tags=["memories"], route_class=BoundedRoute, responses={401: {"model": ErrorBody}}
)


def _conflict(request: Request, stale: core.Stale) -> JSONResponse:
    body = Conflict(
        error=CODES[409],
        message=stale.message,
        request_id=getattr(request.state, "request_id", None),
        current=stale.current,
    )
    return JSONResponse(jsonable_encoder(body.model_dump(exclude={"detail"})), status_code=409)


@router.get("", response_model=Page)
async def list_memories(
    request: Request,
    user: CurrentUser,
    scope: Literal["project", "personal"] | None = None,
    project: Annotated[str | None, Query(pattern=PROJECT_NAME)] = None,
    location: Annotated[str | None, Query(min_length=1, max_length=255)] = None,
    deleted: Annotated[bool, Query(description="include tombstones, as a sync needs")] = False,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    sink: ReadSink = None,
    agent: AgentHandle = None,
) -> Page:
    """The memories the caller sees, oldest change first."""
    return await core.listed(
        request.app.state.engine,
        user,
        scope=scope,
        project=project,
        location=location,
        deleted=deleted,
        handed=cursor,
        limit=limit,
        sink=sink,
        agent=agent,
    )


@router.get("/search", response_model=Results, responses={422: {"model": ErrorBody}})
async def search_memories(
    request: Request,
    user: CurrentUser,
    q: Annotated[str, Query(min_length=1, max_length=500, description='words, "a phrase", or -excluded')],
    scope: Literal["project", "personal"] | None = None,
    project: Annotated[str | None, Query(pattern=PROJECT_NAME)] = None,
    location: Annotated[str | None, Query(min_length=1, max_length=255)] = None,
    limit: Annotated[int, Query(ge=1, le=SEARCH_LIMIT)] = 10,
    sink: ReadSink = None,
    agent: AgentHandle = None,
) -> Results:
    """The memories the caller sees whose name or text matches ``q`` (full text, configuration simple), best
    first."""
    return await core.search(
        request.app.state.engine,
        user,
        q=q,
        scope=scope,
        project=project,
        location=location,
        limit=limit,
        sink=sink,
        agent=agent,
    )


@router.get("/{memory_id}", response_model=Memory, responses={404: {"model": ErrorBody}})
async def show(request: Request, memory_id: MemoryId, user: CurrentUser, sink: ReadSink = None) -> Memory:
    return await core.shown(request.app.state.engine, user, memory_id, sink)


@router.put("", response_model=Written, responses={**REFUSALS, 409: {"model": Conflict}})
async def put(request: Request, body: MemoryIn, user: CurrentUser, sink: Sink = AGENT_SINK) -> Written | JSONResponse:
    """Create or change the memory at the key ``body`` names, in the namespace of ``body.agent`` or the run's."""
    try:
        return await core.put(request.app.state.engine, user, body, sink)
    except core.Stale as stale:
        return _conflict(request, stale)


@router.delete("/{memory_id}", response_model=Written, responses={**REFUSALS, 409: {"model": Conflict}})
async def delete(
    request: Request,
    memory_id: MemoryId,
    user: CurrentUser,
    if_revision: Annotated[int, Query(ge=1, le=MAX_REVISION, description="the revision last seen")],
    sink: Sink = AGENT_SINK,
) -> Written | JSONResponse:
    """Leave a tombstone of memory ``memory_id``: deleted, no body, the next revision. Deleting a tombstone changes
    nothing."""
    try:
        return await core.delete(request.app.state.engine, user, memory_id, if_revision, sink)
    except core.Stale as stale:
        return _conflict(request, stale)


@router.put("/{memory_id}/audience", response_model=Written, responses={**REFUSALS, 409: {"model": Conflict}})
async def lower_audience(
    request: Request, memory_id: MemoryId, body: AudienceIn, user: CurrentUser, sink: Sink = AGENT_SINK
) -> Written | JSONResponse:
    """Lower the audience of memory ``memory_id`` by hand, to a part of it: an admin of its project who reads it
    alone, with the audit row ``memory.audience``. Any other write keeps or raises it (``put``)."""
    try:
        return await core.lower_audience(request.app.state.engine, user, memory_id, body, sink)
    except core.Stale as stale:
        return _conflict(request, stale)


@router.get("/{memory_id}/revisions", response_model=Revisions, responses={404: {"model": ErrorBody}})
async def revisions(
    request: Request,
    memory_id: MemoryId,
    user: CurrentUser,
    before: Annotated[int | None, Query(ge=2, le=MAX_REVISION, description="only revisions older than this")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_REVISIONS)] = DEFAULT_REVISIONS,
    sink: ReadSink = None,
) -> Revisions:
    """The revisions of a memory the caller sees, the latest first. A revision whose label (or type) the caller
    could not read is left out, so a label raised in the past never shows through its history."""
    return await core.revisions(request.app.state.engine, user, memory_id, before, limit, sink)


@router.get("/{memory_id}/revisions/{revision}", response_model=MemoryRevision, responses={404: {"model": ErrorBody}})
async def show_revision(
    request: Request,
    memory_id: MemoryId,
    revision: RevisionNumber,
    user: CurrentUser,
    sink: ReadSink = None,
) -> MemoryRevision:
    """One revision of a memory with its body; 404 when it does not exist or the caller could not read it."""
    return await core.show_revision(request.app.state.engine, user, memory_id, revision, sink)


@restore_router.post("/{memory_id}/restore", response_model=Memory, responses=REFUSALS)
async def restore_memory(request: Request, memory_id: MemoryId, user: CurrentUser, sink: Sink = AGENT_SINK) -> Memory:
    """Bring back an agent's memory the daily upkeep archived: into the index and the searches of its agent's runs
    again, as if just opened, with the audit row ``memory.restore``. A writer of its project who reads it, never the
    agent of a run; a memory not archived is left as it is."""
    return await upkeep.restore(request.app.state.engine, user, memory_id, sink)
