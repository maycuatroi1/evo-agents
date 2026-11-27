"""The SQL of session transcripts on the hub (``evo_agents.hub.server.transcripts``): the blobs a project holds for
them, a transcript locked to take a part, its parts, and the reads of the list, a session, the usage and the
deletions."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import blob_gc, tables
from evo_agents.hub import transcripts as local
from evo_agents.hub.server.runs.models.transcripts import SCAN_ROWS, PartIn


async def blobs_held(conn: AsyncConnection, project_id: int, hashes: list[str]) -> dict[str, int]:
    """{sha256: size} of the blobs of kind transcript among ``hashes`` that the project holds."""
    b = tables.blobs
    query = select(b.c.sha256, b.c.size).where(
        b.c.project_id == project_id, b.c.kind == local.KIND, blob_gc.hashes_in(b.c.sha256, sorted(set(hashes)))
    )
    return {row.sha256: row.size for row in await conn.execute(query)}


async def locked(conn: AsyncConnection, project_id: int, part: PartIn):
    """The transcript of ``part`` locked for update; None for none."""
    t = tables.session_transcripts
    query = (
        select(t)
        .where(t.c.project_id == project_id, t.c.session_id == part.session_id, t.c.file == part.file)
        .with_for_update()
    )
    return (await conn.execute(query)).first()


async def add_transcript(conn: AsyncConnection, values: dict) -> None:
    """A new transcript of ``values``, unless one of its file came first."""
    t = tables.session_transcripts
    await conn.execute(pg_insert(t).values(**values).on_conflict_do_nothing(constraint="session_transcripts_file_key"))


async def same_part(conn: AsyncConnection, transcript_id: int, part: PartIn) -> bool:
    """Whether transcript ``transcript_id`` holds ``part`` already."""
    p = tables.transcript_parts
    query = select(p.c.seq).where(
        p.c.transcript_id == transcript_id,
        p.c.source_start == part.start,
        p.c.source_end == part.end,
        p.c.sha256 == part.sha256,
    )
    return (await conn.execute(query)).first() is not None


async def append(conn: AsyncConnection, row, part: PartIn, totals: dict) -> None:
    """``part`` as the next part of transcript ``row``, whose columns become ``totals``."""
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
    await conn.execute(update(t).values(**totals, updated_at=func.now()).where(t.c.id == row.id))


async def held(conn: AsyncConnection, project_id: int, user_id: int, session_ids: list[str]) -> list:
    """How far the hub holds each transcript of ``user_id`` of ``session_ids``, by session and file."""
    t = tables.session_transcripts
    query = select(t.c.session_id, t.c.file, t.c.runtime, t.c.source_bytes, t.c.lines, t.c.parts, t.c.deleted_at).where(
        t.c.project_id == project_id, t.c.user_id == user_id, t.c.session_id.in_(sorted(set(session_ids)))
    )
    return list((await conn.execute(query.order_by(t.c.session_id, t.c.file))).all())


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


async def summaries(
    conn: AsyncConnection,
    project_id: int,
    *,
    session_id: str | None,
    login: str | None,
    machine: str | None,
    runtime: str | None,
    since: datetime | None,
) -> list:
    """The SCAN_ROWS latest updated transcripts of the project that match the filters given, with their member."""
    t, u = tables.session_transcripts, tables.users
    query = select(*_summary_columns()).join_from(t, u, u.c.id == t.c.user_id)
    query = query.where(t.c.project_id == project_id)
    for column, value in ((t.c.session_id, session_id), (t.c.machine, machine), (t.c.runtime, runtime)):
        if value is not None:
            query = query.where(column == value)
    if login is not None:
        query = query.where(func.lower(u.c.login) == login.lower())
    if since is not None:
        query = query.where(t.c.updated_at >= since)
    query = query.order_by(t.c.updated_at.desc(), t.c.id.desc()).limit(SCAN_ROWS)
    return list((await conn.execute(query)).all())


async def usage_totals(conn: AsyncConnection, project_id: int):
    """The sums over the project's transcripts the usage shows (``TranscriptUsage``)."""
    t = tables.session_transcripts
    live = t.c.deleted_at.is_(None)
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
    ).where(t.c.project_id == project_id)
    return (await conn.execute(totals)).one()


async def blob_bytes(conn: AsyncConnection, project_id: int) -> int:
    """Bytes of the blobs of kind transcript the project holds."""
    b = tables.blobs
    stored = select(func.coalesce(func.sum(b.c.size), 0)).where(b.c.project_id == project_id, b.c.kind == local.KIND)
    return (await conn.execute(stored)).scalar_one()


async def per_member(conn: AsyncConnection, project_id: int) -> list:
    """Per member who uploaded, the sessions, transcripts and bytes of their transcripts still held."""
    t, u = tables.session_transcripts, tables.users
    query = (
        select(
            u.c.login,
            func.count(func.distinct(t.c.session_id)).label("sessions"),
            func.count().label("transcripts"),
            func.coalesce(func.sum(t.c.size), 0).label("bytes"),
        )
        .join_from(t, u, u.c.id == t.c.user_id)
        .where(t.c.project_id == project_id, t.c.deleted_at.is_(None))
        .group_by(u.c.login)
    )
    return list((await conn.execute(query)).all())


async def session_rows(conn: AsyncConnection, project_id: int, session_id: str) -> list:
    """The transcripts of session ``session_id`` (``id`` and the summary's columns)."""
    t, u = tables.session_transcripts, tables.users
    query = (
        select(t.c.id, *_summary_columns())
        .join_from(t, u, u.c.id == t.c.user_id)
        .where(t.c.project_id == project_id, t.c.session_id == session_id)
    )
    return list((await conn.execute(query)).all())


async def parts_of(conn: AsyncConnection, transcript_ids: list[int]) -> list:
    """The parts of transcripts ``transcript_ids``, by transcript and in order."""
    p = tables.transcript_parts
    query = select(p).where(p.c.transcript_id.in_(transcript_ids)).order_by(p.c.transcript_id, p.c.seq)
    return list((await conn.execute(query)).all())


def scope(project_id: int, session_id: str | None):
    """The condition on the project's transcripts, of session ``session_id`` when given."""
    t = tables.session_transcripts
    where = t.c.project_id == project_id
    return where if session_id is None else and_(where, t.c.session_id == session_id)


async def count(conn: AsyncConnection, where) -> int:
    t = tables.session_transcripts
    return (await conn.execute(select(func.count()).select_from(t).where(where))).scalar_one()
