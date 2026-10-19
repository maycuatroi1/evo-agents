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
parts, bytes, per member, and the retention; it needs a grant and reveals counts only.

DELETE .../transcripts/{session_id} and DELETE .../transcripts delete the content of one session's transcripts or of
all the project's, for an admin of the project, with an audit row (``evo_agents.hub.transcript_prune``). The rows
stay, without parts and marked deleted, so a backfill does not upload them again; the blobs no part refers to any
longer leave the bucket (``evo_agents.hub.blob_gc``). The daily job hub.prune_transcripts does the same for the
transcripts that got no part for EVO_HUB_TRANSCRIPT_DAYS days (365 by default), and drops the blobs of kind
transcript that a project holds and no part names, a day after they came. Whatever is computed from transcripts
elsewhere is not touched by either.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import UUID4, AwareDatetime, BaseModel, Field, model_validator
from sqlalchemy import and_, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import blob_gc, digest, tables, transcript_prune
from evo_agents.hub import transcripts as local
from evo_agents.hub.access import has_role
from evo_agents.hub.blobs import KIND_LIMITS
from evo_agents.hub.server import audit
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import LOGIN_NAME, ProjectName
from evo_agents.hub.server.blobs import Mismatch, commit_uploads
from evo_agents.hub.server.digests import BoundedRoute, SessionId
from evo_agents.hub.server.errors import ErrorBody, error_response
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

MAX_PARTS = 100  # parts in one request
MAX_LOOKUP = 1000  # session ids one held request names
DEFAULT_LIMIT = 50
MAX_LIMIT = 500
SCAN_ROWS = 20_000
TRANSCRIPT_DELETE = "transcript.delete"  # an admin deleted transcripts: "<project> session=<id> transcripts=<n>"
STATUSES = ("stored", "held", "conflict", "missing", "refused", "deleted")
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 413, 422, 503)}
READ_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}

FileKey = Annotated[
    str,
    Field(
        pattern=rf"^({local.FILE_KEY.pattern})$",
        max_length=local.MAX_FILE_KEY,
        description="main for the session's own transcript, subagents/... for a subagent's",
    ),
]
Machine = Annotated[str, Field(pattern=r"^[A-Za-z0-9-]{1,40}$", description="the machine, as its hooks name it")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
SessionField = Annotated[str, Field(pattern=rf"^{digest.SESSION_ID.pattern}$")]
Runtime = Literal[local.RUNTIMES]

router = APIRouter(
    prefix="/v1/projects", tags=["transcripts"], route_class=BoundedRoute, responses={401: {"model": ErrorBody}}
)


# Models


class PartIn(BaseModel):
    """One part of a transcript, as ``evo_agents.hub.transcripts.Part.wire`` sends it."""

    session_id: SessionField
    file: FileKey
    runtime: Runtime
    machine: Machine
    cwd: str | None = Field(None, max_length=digest.MAX_CWD, description="the session's directory")
    start: int = Field(ge=0, description="the byte of the file on the machine the part starts at")
    end: int = Field(gt=0, description="the byte after its last")
    first_line: int = Field(ge=1, description="the number of its first line in the file")
    lines: int = Field(ge=1, le=10**8)
    sha256: Sha256 = Field(description="the blob of kind transcript that holds its lines, gzipped")
    size: int = Field(ge=1, le=KIND_LIMITS[local.KIND], description="bytes of that blob")
    raw_size: int = Field(ge=0, le=10**10, description="bytes of its redacted lines before gzip")
    started_at: AwareDatetime | None = Field(None, description="the earliest timestamp of its lines")
    ended_at: AwareDatetime | None = Field(None, description="the latest")

    @model_validator(mode="after")
    def _ordered(self):
        if self.end <= self.start:
            raise ValueError("a part ends after it starts")
        return self


class PartsRequest(BaseModel):
    upload_ids: list[UUID4] = Field(
        default_factory=list, max_length=MAX_PARTS, description="uploads of kind transcript to commit first"
    )
    parts: list[PartIn] = Field(min_length=1, max_length=MAX_PARTS)


class PartOutcome(BaseModel):
    session_id: str
    file: str
    start: int
    end: int
    status: Literal[STATUSES]
    source_bytes: int = Field(description="bytes of the file on the machine the hub holds now")
    lines: int = Field(description="lines of it the hub holds now")


class PartsAnswer(BaseModel):
    parts: list[PartOutcome] = Field(description="in the order of the files and their bytes")
    stored: int = Field(description="parts this request recorded")
    bytes: int = Field(description="bytes of the blobs of those parts")


class HeldRequest(BaseModel):
    session_ids: list[SessionField] = Field(min_length=1, max_length=MAX_LOOKUP)


class HeldTranscript(BaseModel):
    session_id: str
    file: str
    runtime: str
    source_bytes: int
    lines: int
    parts: int
    deleted: bool = Field(description="its content was deleted; the hub takes no more of it")


class HeldAnswer(BaseModel):
    transcripts: list[HeldTranscript] = Field(description="the caller's transcripts of those sessions")


class TranscriptSummary(BaseModel):
    session_id: str
    file: str
    runtime: str
    login: str = Field(description="the member who uploaded it")
    machine: str
    cwd: str | None
    label: dict
    source_bytes: int = Field(description="bytes of the file on the machine the hub holds")
    lines: int
    parts: int
    size: int = Field(description="bytes of its parts in the blob store, gzipped")
    raw_size: int = Field(description="bytes of its redacted lines")
    sha256: str | None = Field(description="the chain of its parts' hashes (evo_agents.hub.transcripts.chain)")
    started_at: datetime | None
    ended_at: datetime | None
    created_at: datetime = Field(description="its first part")
    updated_at: datetime = Field(description="its latest part, or its deletion")
    deleted_at: datetime | None = Field(description="when its content was deleted; null while the hub holds it")


class TranscriptList(BaseModel):
    transcripts: list[TranscriptSummary] = Field(description="the latest updated first")
    total: int = Field(description="transcripts the caller may read that match the filters")
    limit: int
    offset: int


class TranscriptPart(BaseModel):
    seq: int
    source_start: int
    source_end: int
    first_line: int
    lines: int
    sha256: str
    size: int
    raw_size: int
    created_at: datetime


class TranscriptFile(TranscriptSummary):
    part_list: list[TranscriptPart] = Field(description="its parts in order")


class SessionTranscripts(BaseModel):
    project: str
    session_id: str
    transcripts: list[TranscriptFile] = Field(description="the session's own (main) first, then its subagents'")


class MemberUsage(BaseModel):
    login: str
    sessions: int
    transcripts: int
    bytes: int


class TranscriptUsage(BaseModel):
    project: str
    sessions: int = Field(description="sessions whose transcripts the hub holds")
    transcripts: int = Field(description="their transcript files, subagents' included")
    parts: int
    bytes: int = Field(description="bytes of their parts in the blob store, gzipped")
    raw_bytes: int = Field(description="bytes of their redacted lines")
    source_bytes: int = Field(description="bytes of the files on the machines they came from")
    blob_bytes: int = Field(description="bytes of the distinct blobs of kind transcript the project holds")
    deleted: int = Field(description="transcripts whose content was deleted; their rows stay")
    first_at: datetime | None = Field(description="the earliest first part")
    last_at: datetime | None = Field(description="the latest part")
    retention_days: int = Field(description="days a transcript stays after its latest part (EVO_HUB_TRANSCRIPT_DAYS)")
    members: list[MemberUsage] = Field(description="per member who uploaded, the most bytes first")


class TranscriptsDeleted(BaseModel):
    project: str
    session_id: str | None = Field(description="the session; null when every transcript of the project went")
    transcripts: int = Field(description="transcripts whose content was deleted")
    parts: int
    bytes: int


# Writing parts


def _writer(access: ProjectAccess) -> None:
    if not has_role(access.role, "writer"):
        raise HTTPException(403, f"sending transcripts to project {access.name} needs the writer role on it")


def _reader(access: ProjectAccess) -> None:
    if access.role is None:
        raise HTTPException(403, f"reading the transcripts of project {access.name} needs a grant on it")


def _admin(access: ProjectAccess) -> None:
    if not has_role(access.role, "admin"):
        held = access.role or "no grant"
        raise HTTPException(
            403, f"deleting transcripts of project {access.name} needs the admin role on it; you hold {held}"
        )


async def _blobs_held(conn: AsyncConnection, project_id: int, hashes: list[str]) -> dict[str, int]:
    """{sha256: size} of the blobs of kind transcript among ``hashes`` that the project holds."""
    b = tables.blobs
    query = select(b.c.sha256, b.c.size).where(
        b.c.project_id == project_id, b.c.kind == local.KIND, blob_gc.hashes_in(b.c.sha256, sorted(set(hashes)))
    )
    return {row.sha256: row.size for row in await conn.execute(query)}


def _locked(project_id: int, part: PartIn):
    t = tables.session_transcripts
    return (
        select(t)
        .where(t.c.project_id == project_id, t.c.session_id == part.session_id, t.c.file == part.file)
        .with_for_update()
    )


async def _row(conn: AsyncConnection, access: ProjectAccess, user: Principal, label: dict, part: PartIn, sizes):
    """The transcript of ``part`` locked; a new one when the part is its first and its blob is held, else None."""
    row = (await conn.execute(_locked(access.project_id, part))).first()
    if row is not None or part.start != 0 or part.first_line != 1 or sizes.get(part.sha256) != part.size:
        return row
    t = tables.session_transcripts
    values = {
        "project_id": access.project_id,
        "session_id": part.session_id,
        "file": part.file,
        "user_id": user.user_id,
        "runtime": part.runtime,
        "machine": part.machine,
        "cwd": part.cwd,
        "label": label,
    }
    await conn.execute(pg_insert(t).values(**values).on_conflict_do_nothing(constraint="session_transcripts_file_key"))
    return (await conn.execute(_locked(access.project_id, part))).first()


async def _same_part(conn: AsyncConnection, transcript_id: int, part: PartIn) -> bool:
    p = tables.transcript_parts
    query = select(p.c.seq).where(
        p.c.transcript_id == transcript_id,
        p.c.source_start == part.start,
        p.c.source_end == part.end,
        p.c.sha256 == part.sha256,
    )
    return (await conn.execute(query)).first() is not None


def _earlier(a, b):
    return b if a is None else a if b is None else min(a, b)


def _later(a, b):
    return b if a is None else a if b is None else max(a, b)


async def _append(conn: AsyncConnection, row, part: PartIn) -> None:
    t, p = tables.session_transcripts, tables.transcript_parts
    await conn.execute(
        p.insert().values(
            transcript_id=row.id,
            seq=row.parts + 1,
            source_start=part.start,
            source_end=part.end,
            first_line=part.first_line,
            lines=part.lines,
            sha256=part.sha256,
            size=part.size,
            raw_size=part.raw_size,
        )
    )
    await conn.execute(
        update(t)
        .values(
            source_bytes=part.end,
            lines=row.lines + part.lines,
            parts=row.parts + 1,
            size=row.size + part.size,
            raw_size=row.raw_size + part.raw_size,
            sha256=local.chain(row.sha256, part.sha256),
            started_at=_earlier(row.started_at, part.started_at),
            ended_at=_later(row.ended_at, part.ended_at),
            cwd=row.cwd or part.cwd,
            updated_at=func.now(),
        )
        .where(t.c.id == row.id)
    )


async def _store(conn, access, user, label, part: PartIn, sizes: dict[str, int]) -> PartOutcome:
    """Record ``part`` when it continues its transcript (see the module); its outcome either way."""
    row = await _row(conn, access, user, label, part, sizes)

    def outcome(status: str, source: int = row.source_bytes if row else 0, lines: int = row.lines if row else 0):
        return PartOutcome(
            session_id=part.session_id,
            file=part.file,
            start=part.start,
            end=part.end,
            status=status,
            source_bytes=source,
            lines=lines,
        )

    if row is None:
        return outcome("missing" if part.start == 0 and part.first_line == 1 else "conflict")
    if row.user_id != user.user_id:
        return outcome("refused")
    if row.deleted_at is not None:
        return outcome("deleted")
    if part.start != row.source_bytes or part.first_line != row.lines + 1:
        held = part.start < row.source_bytes and await _same_part(conn, row.id, part)
        return outcome("held" if held else "conflict")
    if sizes.get(part.sha256) != part.size:
        return outcome("missing")
    await _append(conn, row, part)
    return outcome("stored", part.end, row.lines + part.lines)


@router.post("/{project}/transcripts/parts", response_model=PartsAnswer, responses=REFUSALS)
async def put_parts(request: Request, project: ProjectName, body: PartsRequest, user: CurrentUser):
    """Commit the uploads of the request, then record each part that continues its transcript."""
    engine = request.app.state.engine
    async with engine.begin() as conn:
        access = await project_access(conn, user, project)
        _writer(access)
        access.push_label()  # the write rule, before any upload is committed
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
    parts = sorted(body.parts, key=lambda part: (part.session_id, part.file, part.start))
    async with engine.begin() as conn:
        access = await project_access(conn, user, project)  # the grant may have changed while the uploads were read
        _writer(access)
        label = access.push_label()
        await blob_gc.lock_shared(conn)  # no deletion between finding a blob held and recording the part
        sizes = await _blobs_held(conn, access.project_id, [part.sha256 for part in parts])
        outcomes = [await _store(conn, access, user, label, part, sizes) for part in parts]
    stored = [part for part, outcome in zip(parts, outcomes, strict=True) if outcome.status == "stored"]
    log.info(
        "transcript parts recorded",
        extra={
            "project": project,
            "login": user.login,
            "parts": len(parts),
            "stored": len(stored),
            "statuses": {status: sum(o.status == status for o in outcomes) for status in STATUSES},
        },
    )
    return PartsAnswer(parts=outcomes, stored=len(stored), bytes=sum(part.size for part in stored))


@router.post("/{project}/transcripts/held", response_model=HeldAnswer, responses=READ_REFUSALS)
async def held_transcripts(request: Request, project: ProjectName, body: HeldRequest, user: CurrentUser):
    """How far the hub holds each of the caller's own transcripts of these sessions."""
    t = tables.session_transcripts
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _writer(access)
        query = select(
            t.c.session_id, t.c.file, t.c.runtime, t.c.source_bytes, t.c.lines, t.c.parts, t.c.deleted_at
        ).where(
            t.c.project_id == access.project_id,
            t.c.user_id == user.user_id,
            t.c.session_id.in_(sorted(set(body.session_ids))),
        )
        rows = (await conn.execute(query.order_by(t.c.session_id, t.c.file))).all()
    found = [HeldTranscript(**row._mapping, deleted=row.deleted_at is not None) for row in rows]
    return HeldAnswer(transcripts=found)


# Reading


def _summary_columns():
    t, u = tables.session_transcripts, tables.users
    return (
        t.c.session_id,
        t.c.file,
        t.c.runtime,
        u.c.login,
        t.c.machine,
        t.c.cwd,
        t.c.label,
        t.c.source_bytes,
        t.c.lines,
        t.c.parts,
        t.c.size,
        t.c.raw_size,
        t.c.sha256,
        t.c.started_at,
        t.c.ended_at,
        t.c.created_at,
        t.c.updated_at,
        t.c.deleted_at,
    )


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
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> TranscriptList:
    """The transcripts of the project the caller may read, the latest updated first."""
    t, u = tables.session_transcripts, tables.users
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        query = select(*_summary_columns()).join_from(t, u, u.c.id == t.c.user_id)
        query = query.where(t.c.project_id == access.project_id)
        for column, value in ((t.c.session_id, session_id), (t.c.machine, machine), (t.c.runtime, runtime)):
            if value is not None:
                query = query.where(column == value)
        if login is not None:
            query = query.where(func.lower(u.c.login) == login.lower())
        if since is not None:
            query = query.where(t.c.updated_at >= since)
        query = query.order_by(t.c.updated_at.desc(), t.c.id.desc()).limit(SCAN_ROWS)
        rows = (await conn.execute(query)).all()
    through = plan_routes._sink(access, sink)
    visible = [TranscriptSummary(**row._mapping) for row in rows if access.visible(row.label, through)]
    return TranscriptList(transcripts=visible[offset : offset + limit], total=len(visible), limit=limit, offset=offset)


@router.get("/{project}/transcripts/usage", response_model=TranscriptUsage, responses=READ_REFUSALS)
async def transcript_usage(request: Request, project: ProjectName, user: CurrentUser) -> TranscriptUsage:
    """What the project's transcripts come to in the blob store, per member, with the retention."""
    t, u, b = tables.session_transcripts, tables.users, tables.blobs
    live = t.c.deleted_at.is_(None)
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        totals = select(
            func.count(func.distinct(t.c.session_id)).filter(live).label("sessions"),
            func.count().filter(live).label("transcripts"),
            func.coalesce(func.sum(t.c.parts).filter(live), 0).label("parts"),
            func.coalesce(func.sum(t.c.size).filter(live), 0).label("bytes"),
            func.coalesce(func.sum(t.c.raw_size).filter(live), 0).label("raw_bytes"),
            func.coalesce(func.sum(t.c.source_bytes).filter(live), 0).label("source_bytes"),
            func.count().filter(t.c.deleted_at.is_not(None)).label("deleted"),
            func.min(t.c.created_at).filter(live).label("first_at"),
            func.max(t.c.updated_at).filter(live).label("last_at"),
        ).where(t.c.project_id == access.project_id)
        found = (await conn.execute(totals)).one()
        stored = select(func.coalesce(func.sum(b.c.size), 0)).where(
            b.c.project_id == access.project_id, b.c.kind == local.KIND
        )
        blob_bytes = (await conn.execute(stored)).scalar_one()
        per_member = (
            select(
                u.c.login,
                func.count(func.distinct(t.c.session_id)).label("sessions"),
                func.count().label("transcripts"),
                func.coalesce(func.sum(t.c.size), 0).label("bytes"),
            )
            .join_from(t, u, u.c.id == t.c.user_id)
            .where(t.c.project_id == access.project_id, live)
            .group_by(u.c.login)
        )
        members = [MemberUsage(**row._mapping) for row in await conn.execute(per_member)]
    members.sort(key=lambda member: (-member.bytes, member.login))
    return TranscriptUsage(
        project=access.name,
        **found._mapping,
        blob_bytes=blob_bytes,
        retention_days=request.app.state.config.transcript_days,
        members=members,
    )


@router.get("/{project}/transcripts/{session_id}", response_model=SessionTranscripts, responses=READ_REFUSALS)
async def show_session(
    request: Request,
    project: ProjectName,
    session_id: SessionId,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> SessionTranscripts:
    """The transcripts of session ``session_id`` the caller may read, with their parts."""
    t, u, p = tables.session_transcripts, tables.users, tables.transcript_parts
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        query = (
            select(t.c.id, *_summary_columns())
            .join_from(t, u, u.c.id == t.c.user_id)
            .where(t.c.project_id == access.project_id, t.c.session_id == session_id)
        )
        through = plan_routes._sink(access, sink)
        rows = [row for row in (await conn.execute(query)).all() if access.visible(row.label, through)]
        held = select(p).where(p.c.transcript_id.in_([row.id for row in rows])).order_by(p.c.transcript_id, p.c.seq)
        parts: dict[int, list[TranscriptPart]] = {}
        for part in (await conn.execute(held)).all() if rows else []:
            parts.setdefault(part.transcript_id, []).append(TranscriptPart(**part._mapping))
    if not rows:
        raise HTTPException(
            404,
            f"project {project} has no transcript of session {session_id} that you may read: see "
            f"GET /v1/projects/{project}/transcripts",
        )
    files = [
        TranscriptFile(
            **{key: value for key, value in row._mapping.items() if key != "id"}, part_list=parts.get(row.id, [])
        )
        for row in rows
    ]
    files.sort(key=lambda item: (item.file != local.MAIN, item.file))
    return SessionTranscripts(project=access.name, session_id=session_id, transcripts=files)


# Deleting


async def _delete(request: Request, user: Principal, project: str, session_id: str | None) -> TranscriptsDeleted:
    t = tables.session_transcripts
    engine = request.app.state.engine
    async with engine.begin() as conn:
        access = await project_access(conn, user, project)
        _admin(access)
        where = t.c.project_id == access.project_id
        if session_id is not None:
            where = and_(where, t.c.session_id == session_id)
            known = select(func.count()).select_from(t).where(where)
            if not (await conn.execute(known)).scalar_one():
                raise HTTPException(404, f"project {project} has no transcript of session {session_id}")
        await blob_gc.lock_exclusive(conn)
        dropped = await transcript_prune.drop(conn, where)
        target = (
            f"{access.name} "
            + (f"session={session_id}" if session_id else "all")
            + f" transcripts={dropped.transcripts}"
        )
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=TRANSCRIPT_DELETE,
            target=target,
            project_id=access.project_id,
        )
    await transcript_prune.delete_objects(engine, request.app.state.blobs)
    log.info(
        "transcripts deleted",
        extra={"project": access.name, "login": user.login, "session_id": session_id, **asdict(dropped)},
    )
    return TranscriptsDeleted(project=access.name, session_id=session_id, **asdict(dropped))


@router.delete("/{project}/transcripts/{session_id}", response_model=TranscriptsDeleted, responses=READ_REFUSALS)
async def delete_session(request: Request, project: ProjectName, session_id: SessionId, user: CurrentUser):
    """Delete the content of the transcripts of session ``session_id``; an admin of the project only."""
    return await _delete(request, user, project, session_id)


@router.delete("/{project}/transcripts", response_model=TranscriptsDeleted, responses=READ_REFUSALS)
async def delete_project_transcripts(request: Request, project: ProjectName, user: CurrentUser):
    """Delete the content of every transcript of the project; an admin of the project only."""
    return await _delete(request, user, project, None)
