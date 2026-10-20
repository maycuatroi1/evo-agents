"""Signals: a service or a member sends a batch, members read them by key and time (``evo_agents.hub.signals``).

POST /v1/signals/{project} takes a batch of at most 500 items in at most 1 MiB of body, from an ingest key of the
project (``Authorization: Bearer evi_...``, which the middleware lets reach this route alone) or from a member with the
writer role, with their machine token or web session. It answers 202 with the batch's id, how many signals it stored,
how many repeated an ``external_id`` stored before, and each item refused with its index and why; the others are
stored. A body over the limit is a 413, read no further; a key past its rate or a project past its day is a 429 with
Retry-After; an ingest key revoked or unknown is a 401, and one of another project a 403. A project whose knowledge.yaml
declares no hub sink takes no signal (422), as it takes no memory.

GET /v1/projects/{project}/signals reads them for a member with a grant: the newest first, of one key or all, in a range
of measured times, of one tenant, at most ``limit``. A signal is read under its label through the sink X-Evo-Sink
names, else the project's hub sink, as plans are; one above what the member may read is left out. The value of a
signal never goes into a log line.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import AwareDatetime, BaseModel, Field

from evo_agents.hub import signals
from evo_agents.hub.access import has_role
from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.errors import ErrorBody, error_response
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import principal
from evo_agents.hub.tables_signals import (
    MAX_EXTERNAL_ID_CHARS,
    MAX_KEY_NAME_CHARS,
    MAX_LEVEL_CHARS,
    MAX_SOURCE_CHARS,
    MAX_TENANT_CHARS,
    MAX_TEXT_BYTES,
)

SINK_HEADER = "X-Evo-Sink"  # evo_agents.hub.server.plans.SINK_HEADER
PRINTABLE = r"^[^\x00-\x1f\x7f]+$"
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
TOO_LARGE = f"a batch of signals is at most {signals.MAX_BATCH_BYTES // 1024} KiB of request"
PUSH_REFUSALS = {code: {"model": ErrorBody} for code in (401, 403, 404, 413, 422, 429)}
READ_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}
ProjectName = Annotated[str, Path(pattern=PROJECT_NAME)]
Sink = Annotated[str | None, Header(alias=SINK_HEADER)]
Printable = Annotated[str, Field(pattern=PRINTABLE)]


class BoundedRoute(APIRoute):
    """Reads at most MAX_BATCH_BYTES of a request before FastAPI parses it: a larger one is a 413, whether it says its
    length or not (as ``evo_agents.hub.server.digests.BoundedRoute`` does for digests)."""

    def get_route_handler(self):
        handler = super().get_route_handler()
        limit = signals.MAX_BATCH_BYTES

        async def bounded(request: Request):
            declared = request.headers.get("content-length")
            if declared is not None and (not declared.isdigit() or int(declared) > limit):
                raise HTTPException(413, TOO_LARGE)
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, TOO_LARGE)
                chunks.append(chunk)
            request._body = b"".join(chunks)  # what Request.body() returns from now on
            return await handler(request)

        return bounded


push_router = APIRouter(prefix="/v1/signals", tags=["signals"], route_class=BoundedRoute)
router = APIRouter(prefix="/v1/projects", tags=["signals"], responses={401: {"model": ErrorBody}})


# Models


class SignalIn(BaseModel):
    key: str = Field(min_length=1, max_length=MAX_KEY_NAME_CHARS, description="a key of the project's registry")
    value: float | None = Field(default=None, allow_inf_nan=False, description="the number, for a key of kind number")
    text: str | None = Field(
        default=None,
        max_length=MAX_TEXT_BYTES,
        description=f"the feedback, for a key of kind feedback: at most {MAX_TEXT_BYTES // 1024} KiB in UTF-8",
    )
    source: Printable | None = Field(
        default=None, max_length=MAX_SOURCE_CHARS, description="who measured it; the key's source when left out"
    )
    measured_at: AwareDatetime | None = Field(default=None, description="when it was measured; now when left out")
    tenant: Printable | None = Field(
        default=None, max_length=MAX_TENANT_CHARS, description="who it is about, such as a customer; free text"
    )
    synthetic: bool = Field(default=False, description="it comes from simulated people, not real ones")
    external_id: Printable | None = Field(
        default=None,
        max_length=MAX_EXTERNAL_ID_CHARS,
        description="the sender's id of it: sent again with the same key, it is stored once",
    )
    level: Printable | None = Field(
        default=None,
        max_length=MAX_LEVEL_CHARS,
        description="its sensitivity, a level of the project's ladder; the key's, else the project's default",
    )


class SignalBatchIn(BaseModel):
    items: list[SignalIn] = Field(min_length=1, max_length=signals.MAX_ITEMS)


class SignalRejection(BaseModel):
    index: int = Field(description="the item's place in the batch, 0 first")
    key: str
    external_id: str | None
    reason: str


class SignalBatchResult(BaseModel):
    batch_id: UUID
    accepted: int = Field(description="signals stored")
    duplicates: int = Field(description="items whose external_id this key or member had sent before")
    rejected: list[SignalRejection]


class Signal(BaseModel):
    id: int
    key: str
    kind: Literal["number", "feedback"]
    value: float | None
    text: str | None
    source: str
    measured_at: datetime
    tenant: str | None
    synthetic: bool
    external_id: str | None
    label: dict
    via: Literal["ingest", "member", "run"] = Field(description="who pushed it: an ingest key, a member, a run")
    ingest_key: str | None = Field(description="the ingest key's name, when one pushed it")
    login: str | None = Field(description="the member who pushed it, or for whom a run did")
    run_id: int | None
    batch_id: UUID
    received_at: datetime


# Pushing


async def _member(request: Request, project: str) -> signals.Pusher:
    """The pusher of a batch a member sends: the writer role and a hub sink that takes the project's default label,
    as for any push."""
    user = principal(request)
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        access.push_label(None)
    scope = user.scope
    via = "member" if scope is None else "run"
    run_id = None if scope is None else scope.run_id
    return signals.Pusher(access.project_id, access.rules, via, user_id=user.user_id, run_id=run_id)


@push_router.post("/{project}", status_code=202, response_model=SignalBatchResult, responses=PUSH_REFUSALS)
async def push_signals(request: Request, project: ProjectName, batch: SignalBatchIn):
    """Store a batch of signals of the project, sent with one of its ingest keys or by a writer: each item checked on
    its own, the refused ones answered with why."""
    try:
        token = getattr(request.state, "ingest_token", None)
        if token is not None:
            pusher = await signals.open_ingest(request.app.state.engine, token, project)
        else:
            pusher = await _member(request, project)
        async with request.app.state.engine.begin() as conn:
            done = await signals.push(conn, pusher, batch.items, request.app.state.config.signal_days)
    except signals.SignalError as exc:
        return error_response(request, exc.status, exc.message, headers=exc.headers)
    body = SignalBatchResult(
        batch_id=done.batch_id,
        accepted=done.accepted,
        duplicates=done.duplicates,
        rejected=[SignalRejection(**item) for item in done.rejected],
    )
    return JSONResponse(body.model_dump(mode="json"), status_code=202)


# Reading


def readable(access: ProjectAccess, sink: str | None) -> list[tuple[str, str]]:
    """The (level, location) pairs of the project's ladder that a label of this project with them may be read at,
    by the member, through ``sink`` or else the project's hub sink."""
    through = sink or (access.rules.hub_sink.sink if access.rules.hub_sink else None)
    if through is None:
        return []
    rules = access.rules
    return [
        (level, location)
        for level in rules.levels
        for location in rules.locations
        if access.visible({"level": level, "location": location, "integrity": "U", "projects": [rules.name]}, through)
    ]


@router.get("/{project}/signals", response_model=list[Signal], responses=READ_REFUSALS)
async def list_signals(
    request: Request,
    project: ProjectName,
    sink: Sink = None,
    key: Annotated[str | None, Query(max_length=MAX_KEY_NAME_CHARS, description="one key; all when left out")] = None,
    since: Annotated[AwareDatetime | None, Query(description="measured at or after")] = None,
    until: Annotated[AwareDatetime | None, Query(description="measured before")] = None,
    tenant: Annotated[str | None, Query(max_length=MAX_TENANT_CHARS)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> list[Signal]:
    """The project's signals the member may read, newest first."""
    user = principal(request)
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        if not has_role(access.role, "reader"):
            raise HTTPException(403, f"reading the signals of project {project} needs a grant on it")
        pairs = readable(access, sink)
        rows = await signals.signal_rows(
            conn, access.project_id, pairs, key=key, since=since, until=until, tenant=tenant, limit=limit
        )
    through = sink or (access.rules.hub_sink.sink if access.rules.hub_sink else "")
    return [Signal(**row._mapping) for row in rows if access.visible(row.label, through)]
