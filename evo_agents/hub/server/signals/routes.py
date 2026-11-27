"""The routes of the signals of a project: the batches an ingest key or a member pushes (``push_router``, whose auth
the route reads itself), the members' reads of them (``router``), and the registry of keys and the ingest keys
(``keys_router``). Each route of a project reads its scope (``service.READERS``, ``service.KEY_READERS`` and the
like) and hands the request to the service."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import AwareDatetime

from evo_agents.hub import signals
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.base.scope import Scope
from evo_agents.hub.server.errors import ErrorBody, error_response, refusals
from evo_agents.hub.server.security import principal
from evo_agents.hub.server.signals import service
from evo_agents.hub.server.signals.models import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    SINK_HEADER,
    TOO_LARGE,
    IngestId,
    KeyName,
    Signal,
    SignalBatchIn,
    SignalBatchResult,
    SignalIngestKey,
    SignalIngestKeyIn,
    SignalIngestKeyIssued,
    SignalKey,
    SignalKeyIn,
    SignalRejection,
)
from evo_agents.hub.tables_signals import MAX_KEY_NAME_CHARS, MAX_TENANT_CHARS

PUSH_REFUSALS = refusals(401, 403, 404, 413, 422, 429)
READ_REFUSALS = refusals(403, 404)
REFUSALS = refusals(403, 404, 409, 422)
Sink = Annotated[str | None, Header(alias=SINK_HEADER)]


class BoundedRoute(APIRoute):
    """Reads at most MAX_BATCH_BYTES of a request before FastAPI parses it: a larger one is a 413, whether it says its
    length or not (as ``evo_agents.hub.server.digests.models.sessions.BoundedRoute`` does for digests)."""

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
keys_router = APIRouter(prefix="/v1/projects", tags=["signals"], responses={401: {"model": ErrorBody}})

Reading = Annotated[Scope, Depends(service.READERS, scope="function")]


# Pushing


@push_router.post("/{project}", status_code=202, response_model=SignalBatchResult, responses=PUSH_REFUSALS)
async def push_signals(request: Request, project: ProjectName, batch: SignalBatchIn):
    """Store a batch of signals of the project, sent with one of its ingest keys or by a writer: each item checked on
    its own, the refused ones answered with why."""
    try:
        token = getattr(request.state, "ingest_token", None)
        user = principal(request) if token is None else None
        state = request.app.state
        done = await service.push(state.engine, token, user, project, batch.items, state.config.signal_days)
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


@router.get("/{project}/signals", response_model=list[Signal], responses=READ_REFUSALS)
async def list_signals(
    project: ProjectName,
    scope: Reading,
    sink: Sink = None,
    key: Annotated[str | None, Query(max_length=MAX_KEY_NAME_CHARS, description="one key; all when left out")] = None,
    since: Annotated[AwareDatetime | None, Query(description="measured at or after")] = None,
    until: Annotated[AwareDatetime | None, Query(description="measured before")] = None,
    tenant: Annotated[str | None, Query(max_length=MAX_TENANT_CHARS)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> list[Signal]:
    """The project's signals the member may read, newest first."""
    return await service.listed(scope, sink, key=key, since=since, until=until, tenant=tenant, limit=limit)


# The registry of keys


@keys_router.get("/{project}/signal-keys", response_model=list[SignalKey], responses=REFUSALS)
async def list_keys(
    project: ProjectName,
    scope: Annotated[Scope, Depends(service.KEY_READERS, scope="function")],
    archived: Annotated[bool, Query(description="archived keys too")] = False,
) -> list[SignalKey]:
    """The keys of the project's registry, by name."""
    return await service.keys(scope, archived)


@keys_router.post("/{project}/signal-keys", status_code=201, response_model=SignalKey, responses=REFUSALS)
async def add_key(
    project: ProjectName, body: SignalKeyIn, scope: Annotated[Scope, Depends(service.KEY_ADDERS, scope="function")]
) -> SignalKey:
    """Add a key to the project's registry: from now on the hub takes signals of it."""
    return await service.add_key(scope, body)


@keys_router.post("/{project}/signal-keys/{name}/archive", response_model=SignalKey, responses=REFUSALS)
async def archive_key(
    project: ProjectName, name: KeyName, scope: Annotated[Scope, Depends(service.KEY_ARCHIVERS, scope="function")]
) -> SignalKey:
    """Archive a key: it takes no new signal, and its signals stay. Archiving it again changes nothing."""
    return await service.archive_key(scope, name)


# Ingest keys


@keys_router.get("/{project}/signal-ingest-keys", response_model=list[SignalIngestKey], responses=REFUSALS)
async def list_ingest_keys(
    project: ProjectName,
    scope: Annotated[Scope, Depends(service.INGEST_READERS, scope="function")],
    revoked: Annotated[bool, Query(description="revoked keys too")] = False,
) -> list[SignalIngestKey]:
    """The project's ingest keys, oldest first; never a token."""
    return await service.ingest_keys(scope, revoked)


@keys_router.post(
    "/{project}/signal-ingest-keys", status_code=201, response_model=SignalIngestKeyIssued, responses=REFUSALS
)
async def create_ingest_key(
    project: ProjectName,
    body: SignalIngestKeyIn,
    scope: Annotated[Scope, Depends(service.INGEST_CREATORS, scope="function")],
) -> SignalIngestKeyIssued:
    """A new ingest key for a service; its token is in this answer and nowhere else."""
    return await service.create_ingest_key(scope, body)


@keys_router.post("/{project}/signal-ingest-keys/{key_id}/revoke", response_model=SignalIngestKey, responses=REFUSALS)
async def revoke_ingest_key(
    project: ProjectName,
    key_id: IngestId,
    scope: Annotated[Scope, Depends(service.INGEST_REVOKERS, scope="function")],
) -> SignalIngestKey:
    """Revoke an ingest key: the next batch sent with it gets 401. Revoking it again changes nothing."""
    return await service.revoke_ingest_key(scope, key_id)
