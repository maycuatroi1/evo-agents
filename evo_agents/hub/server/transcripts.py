"""Session transcripts: the whole transcript of every Claude Code and Codex session of a project's directory, which the
evo-hub plugin sends in parts as the session goes on (``evo_agents.hub.transcripts`` cuts and redacts them on the
machine, ``evo_agents.hub.transcript_sync`` sends them).

A part's lines live in the blob store, one blob of kind transcript each (gzip of the redacted lines), which the client
uploads through POST /v1/blobs/uploads and the presigned PUT as any blob. Postgres holds the index:
``session_transcripts``, a row per transcript file (the session's own, ``main``, and each subagent's), and
``transcript_parts``, its parts in order.

POST /v1/projects/{project}/transcripts/parts commits the uploads it names (as POST /v1/blobs/commit does, kind
transcript only) and then records each part, under the write rule of the project: the writer role, and a hub sink that
clears the project's default label, which every transcript carries. A part continues its file: it starts at the byte
of the file on the machine where the hub's copy ends (``source_bytes``) and at the line after its last. Each part comes
back with a status and what the hub holds of the file then, so a client that lost its place learns it:

- ``stored``: recorded;
- ``held``: the hub holds this very part already (a request sent again after its answer was lost); nothing changes;
- ``conflict``: it does not start where the hub's copy ends; send again from ``source_bytes``;
- ``missing``: the project holds no blob of that hash and size; upload it and send the part again;
- ``refused``: another member uploaded this transcript first, and only they add to it;
- ``deleted``: an admin or the retention deleted this transcript; the hub takes no more of it.

So a part sent twice is stored once, and a client that went down mid-way loses nothing. POST
.../transcripts/held answers, for the session ids given, how far the hub holds each of the caller's transcripts of
them, which a backfill reads before it sends anything.

GET /v1/projects/{project}/transcripts lists the transcripts the caller may read, the latest updated first, filtered
by session, member, machine, runtime and ``since`` (updated at or after); GET .../transcripts/{session_id} is one
session's with their parts. Reading needs a grant on the project and goes through the read rule with the
transcript's label, through the sink X-Evo-Sink names, else the project's hub sink, as digests are read; a list
examines at most SCAN_ROWS rows. GET .../transcripts/usage counts what the project holds: sessions, transcripts,
parts, bytes, per member, and the retention; it needs a grant whose reach takes the project's default label through
that sink (404 otherwise, as the night's figures), and reveals counts only.

DELETE .../transcripts/{session_id} and DELETE .../transcripts delete the content of one session's transcripts or of
all the project's, for an admin of the project, with an audit row (``evo_agents.hub.transcript_prune``). The rows
stay, without parts and marked deleted, so a backfill does not upload them again; the blobs no part refers to any
longer leave the bucket (``evo_agents.hub.blob_gc``). The daily job hub.prune_transcripts does the same for the
transcripts that got no part for EVO_HUB_TRANSCRIPT_DAYS days (365 by default), and drops the blobs of kind
transcript that a project holds and no part names, a day after they came. Whatever is computed from transcripts
elsewhere is not touched by either.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Query, Request
from pydantic import AwareDatetime

from evo_agents.hub import digest
from evo_agents.hub import transcripts as local
from evo_agents.hub.server.base.models import LOGIN_NAME, ProjectName
from evo_agents.hub.server.blobs import Mismatch, commit_uploads
from evo_agents.hub.server.digests.models.sessions import BoundedRoute, SessionId
from evo_agents.hub.server.errors import ErrorBody, error_response
from evo_agents.hub.server.plans import models as plan_models
from evo_agents.hub.server.runs.models.transcripts import DEFAULT_LIMIT, MAX_LIMIT, SCAN_ROWS, Runtime
from evo_agents.hub.server.runs.models.transcripts import HeldAnswer as HeldAnswer
from evo_agents.hub.server.runs.models.transcripts import HeldRequest as HeldRequest
from evo_agents.hub.server.runs.models.transcripts import PartsAnswer as PartsAnswer
from evo_agents.hub.server.runs.models.transcripts import PartsRequest as PartsRequest
from evo_agents.hub.server.runs.models.transcripts import SessionTranscripts as SessionTranscripts
from evo_agents.hub.server.runs.models.transcripts import TranscriptList as TranscriptList
from evo_agents.hub.server.runs.models.transcripts import TranscriptsDeleted as TranscriptsDeleted
from evo_agents.hub.server.runs.models.transcripts import TranscriptUsage as TranscriptUsage
from evo_agents.hub.server.runs.service import transcripts as service
from evo_agents.hub.server.security import CurrentUser

REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 413, 422, 503)}
READ_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}

router = APIRouter(
    prefix="/v1/projects", tags=["transcripts"], route_class=BoundedRoute, responses={401: {"model": ErrorBody}}
)


@router.post("/{project}/transcripts/parts", response_model=PartsAnswer, responses=REFUSALS)
async def put_parts(request: Request, project: ProjectName, body: PartsRequest, user: CurrentUser):
    """Commit the uploads of the request, then record each part that continues its transcript."""
    engine = request.app.state.engine
    await service.may_send(engine, user, project)  # the write rule, before any upload is committed
    if body.upload_ids:
        try:
            ids = [str(upload_id) for upload_id in body.upload_ids]
            await commit_uploads(request, user, project, ids, kinds=frozenset({local.KIND}))
        except Mismatch as exc:
            message = (
                f"{len(exc.problems)} upload(s) do not match what was declared, such as "
                f"{exc.problems[0]['upload_id']}: {exc.problems[0]['problem']}; no part was recorded"
            )
            return error_response(request, 422, message, detail=exc.problems)
    return await service.store_parts(engine, user, project, body.parts)


@router.post("/{project}/transcripts/held", response_model=HeldAnswer, responses=READ_REFUSALS)
async def held_transcripts(request: Request, project: ProjectName, body: HeldRequest, user: CurrentUser):
    """How far the hub holds each of the caller's own transcripts of these sessions."""
    return await service.held_transcripts(request.app.state.engine, user, project, body.session_ids)


@router.get("/{project}/transcripts", response_model=TranscriptList, responses=READ_REFUSALS)
async def list_transcripts(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    session_id: Annotated[str | None, Query(pattern=rf"^{digest.SESSION_ID.pattern}$")] = None,
    login: Annotated[str | None, Query(pattern=LOGIN_NAME, description="uploaded by this member")] = None,
    machine: Annotated[str | None, Query(pattern=r"^[A-Za-z0-9-]{1,40}$")] = None,
    runtime: Annotated[Runtime | None, Query()] = None,
    since: Annotated[AwareDatetime | None, Query(description="updated at or after this time")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0, le=SCAN_ROWS)] = 0,
    sink: Annotated[str | None, Header(alias=plan_models.SINK_HEADER)] = None,
) -> TranscriptList:
    """The transcripts of the project the caller may read, the latest updated first."""
    filters = {"session_id": session_id, "login": login, "machine": machine, "runtime": runtime, "since": since}
    return await service.list_transcripts(request.app.state.engine, user, project, filters, limit, offset, sink)


@router.get("/{project}/transcripts/usage", response_model=TranscriptUsage, responses=READ_REFUSALS)
async def transcript_usage(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_models.SINK_HEADER)] = None,
) -> TranscriptUsage:
    """What the project's transcripts come to in the blob store, per member, with the retention: for a reader who
    reads the project's default label through the sink (the project's hub sink by default), as the night's figures
    are."""
    days = request.app.state.config.transcript_days
    return await service.usage(request.app.state.engine, user, project, sink, days)


@router.get("/{project}/transcripts/{session_id}", response_model=SessionTranscripts, responses=READ_REFUSALS)
async def show_session(
    request: Request,
    project: ProjectName,
    session_id: SessionId,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_models.SINK_HEADER)] = None,
) -> SessionTranscripts:
    """The transcripts of session ``session_id`` the caller may read, with their parts."""
    return await service.show_session(request.app.state.engine, user, project, session_id, sink)


@router.delete("/{project}/transcripts/{session_id}", response_model=TranscriptsDeleted, responses=READ_REFUSALS)
async def delete_session(request: Request, project: ProjectName, session_id: SessionId, user: CurrentUser):
    """Delete the content of the transcripts of session ``session_id``; an admin of the project only."""
    state = request.app.state
    return await service.delete(state.engine, state.blobs, user, project, session_id)


@router.delete("/{project}/transcripts", response_model=TranscriptsDeleted, responses=READ_REFUSALS)
async def delete_project_transcripts(request: Request, project: ProjectName, user: CurrentUser):
    """Delete the content of every transcript of the project; an admin of the project only."""
    state = request.app.state
    return await service.delete(state.engine, state.blobs, user, project, None)
