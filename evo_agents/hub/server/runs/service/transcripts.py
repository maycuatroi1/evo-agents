"""Session transcripts on the hub (``evo_agents.hub.server.transcripts``): who may send, read and delete them, how a
part continues its transcript, and the reads of the list, a session and the usage."""

from __future__ import annotations

import logging
from dataclasses import asdict

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import blob_gc, transcript_prune
from evo_agents.hub import transcripts as local
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.plans import service as plan_service
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.runs.models.transcripts import (
    STATUSES,
    TRANSCRIPT_DELETE,
    HeldAnswer,
    HeldTranscript,
    MemberUsage,
    PartIn,
    PartOutcome,
    PartsAnswer,
    SessionTranscripts,
    TranscriptFile,
    TranscriptList,
    TranscriptPart,
    TranscriptsDeleted,
    TranscriptSummary,
    TranscriptUsage,
)
from evo_agents.hub.server.runs.repository import transcripts as repository
from evo_agents.hub.server.security import Principal, holds

log = logging.getLogger("evo_agents.hub.server.transcripts")


def need_writer(access: ProjectAccess) -> None:
    if not holds(access.role, "writer"):
        raise HTTPException(403, f"sending transcripts to project {access.name} needs the writer role on it")


def need_reader(access: ProjectAccess) -> None:
    if access.role is None:
        raise HTTPException(403, f"reading the transcripts of project {access.name} needs a grant on it")


def need_admin(access: ProjectAccess) -> None:
    if not holds(access.role, "admin"):
        held = access.role or "no grant"
        raise HTTPException(
            403, f"deleting transcripts of project {access.name} needs the admin role on it; you hold {held}"
        )


# Writing parts


async def may_send(engine: AsyncEngine, user: Principal, project: str) -> None:
    """Refuse a caller the write rule of the project refuses, before any upload is committed."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        need_writer(access)
        access.push_label()


async def _row(conn: AsyncConnection, access: ProjectAccess, user: Principal, label: dict, part: PartIn, sizes):
    """The transcript of ``part`` locked; a new one when the part is its first and its blob is held, else None."""
    row = await repository.locked(conn, access.project_id, part)
    if row is not None or part.start != 0 or part.first_line != 1 or sizes.get(part.sha256) != part.size:
        return row
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
    await repository.add_transcript(conn, values)
    return await repository.locked(conn, access.project_id, part)


def _earlier(a, b):
    return b if a is None else a if b is None else min(a, b)


def _later(a, b):
    return b if a is None else a if b is None else max(a, b)


def _totals(row, part: PartIn) -> dict:
    """The columns of transcript ``row`` once ``part`` continues it."""
    return {
        "source_bytes": part.end,
        "lines": row.lines + part.lines,
        "parts": row.parts + 1,
        "size": row.size + part.size,
        "raw_size": row.raw_size + part.raw_size,
        "sha256": local.chain(row.sha256, part.sha256),
        "started_at": _earlier(row.started_at, part.started_at),
        "ended_at": _later(row.ended_at, part.ended_at),
        "cwd": row.cwd or part.cwd,
    }


async def _store(conn, access, user, label, part: PartIn, sizes: dict[str, int]) -> PartOutcome:
    """Record ``part`` when it continues its transcript (see ``evo_agents.hub.server.transcripts``); its outcome
    either way."""
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
        held = part.start < row.source_bytes and await repository.same_part(conn, row.id, part)
        return outcome("held" if held else "conflict")
    if sizes.get(part.sha256) != part.size:
        return outcome("missing")
    await repository.append(conn, row, part, _totals(row, part))
    return outcome("stored", part.end, row.lines + part.lines)


async def store_parts(engine: AsyncEngine, user: Principal, project: str, parts: list[PartIn]) -> PartsAnswer:
    """Record each part that continues its transcript, once the uploads of the request are committed."""
    parts = sorted(parts, key=lambda part: (part.session_id, part.file, part.start))
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)  # the grant may have changed while the uploads were read
        need_writer(access)
        label = access.push_label()
        await blob_gc.lock_shared(conn)  # no deletion between finding a blob held and recording the part
        sizes = await repository.blobs_held(conn, access.project_id, [part.sha256 for part in parts])
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


async def held_transcripts(engine: AsyncEngine, user: Principal, project: str, session_ids: list[str]) -> HeldAnswer:
    """How far the hub holds each of the caller's own transcripts of ``session_ids``."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        need_writer(access)
        rows = await repository.held(conn, access.project_id, user.user_id, session_ids)
    found = [HeldTranscript(**row._mapping, deleted=row.deleted_at is not None) for row in rows]
    return HeldAnswer(transcripts=found)


# Reading


async def list_transcripts(
    engine: AsyncEngine, user: Principal, project: str, filters: dict, limit: int, offset: int, sink: str | None
) -> TranscriptList:
    """The transcripts of the project the caller may read that match ``filters`` (the keywords of
    ``repository.summaries``), the latest updated first."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        need_reader(access)
        rows = await repository.summaries(conn, access.project_id, **filters)
    through = plan_service.read_sink(access, sink)
    visible = [TranscriptSummary(**row._mapping) for row in rows if access.visible(row.label, through)]  # pyright: ignore[reportArgumentType]
    return TranscriptList(transcripts=visible[offset : offset + limit], total=len(visible), limit=limit, offset=offset)


async def usage(
    engine: AsyncEngine, user: Principal, project: str, sink: str | None, retention_days: int
) -> TranscriptUsage:
    """What the project's transcripts come to, for a reader who reads its default label through the sink."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        need_reader(access)
        if not plan_service.sees(access, access.rules.default_label, sink):
            raise HTTPException(404, f"project {project} has no usage of transcripts you may read through this sink")
        found = await repository.usage_totals(conn, access.project_id)
        blob_bytes = await repository.blob_bytes(conn, access.project_id)
        members = [MemberUsage(**row._mapping) for row in await repository.per_member(conn, access.project_id)]
    members.sort(key=lambda member: (-member.bytes, member.login))
    return TranscriptUsage(
        project=access.name, **found._mapping, blob_bytes=blob_bytes, retention_days=retention_days, members=members
    )


async def show_session(
    engine: AsyncEngine, user: Principal, project: str, session_id: str, sink: str | None
) -> SessionTranscripts:
    """The transcripts of session ``session_id`` the caller may read, with their parts."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        need_reader(access)
        through = plan_service.read_sink(access, sink)
        found = await repository.session_rows(conn, access.project_id, session_id)
        rows = [row for row in found if access.visible(row.label, through)]  # pyright: ignore[reportArgumentType]
        parts: dict[int, list[TranscriptPart]] = {}
        for part in await repository.parts_of(conn, [row.id for row in rows]) if rows else []:
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


async def delete(
    engine: AsyncEngine, blobs, user: Principal, project: str, session_id: str | None
) -> TranscriptsDeleted:
    """Delete the content of session ``session_id``'s transcripts, or of all the project's for None, with an audit
    row; then the blobs no part names any longer leave ``blobs``."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        need_admin(access)
        where = repository.scope(access.project_id, session_id)
        if session_id is not None and not await repository.count(conn, where):
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
    await transcript_prune.delete_objects(engine, blobs)
    log.info(
        "transcripts deleted",
        extra={"project": access.name, "login": user.login, "session_id": session_id, **asdict(dropped)},
    )
    return TranscriptsDeleted(project=access.name, session_id=session_id, **asdict(dropped))
