"""The job hub.local_spend: what a local run spent, read from the transcripts the hub holds of its session.

A local run (``runs.kind`` local, a Claude Code session on a member's machine) shows its spend from the usage reports
its machine sends (``evo_agents.hub.local_usage``). One whose machine sent none, every session before the evo-hub
plugin read the transcript's usage and any of a machine still on such a plugin, has its session's transcripts on the
hub all the same (``session_transcripts``, their parts in the blob store). Each pass, every 5 minutes, takes up to
PASS_ITEMS such runs, the latest first, for at most PASS_SECONDS: a local run with no ``usage_update`` event whose
session's transcripts, quiet for SETTLE, have another fingerprint (``facet_job``'s) than the one it was read from
(``runs.spend_transcripts``). It reads them as the hook does, the subagents' files before the session's own
(``local_usage.UsageReader``), and writes the run's spend from those reports (``spend_store.write``): each model's
tokens, Claude Code's ``cost-state`` as reported, the calls after it at list prices. A blob the store cannot give is
logged and the fingerprint kept, so the run is read again when its transcripts change. The job is queued once at a
time and runs one at a time.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path

from sqlalchemy import and_, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub import facet_job, local_usage, run_spend, spend_store, tables
from evo_agents.hub import transcripts as local
from evo_agents.hub.blobs import BlobStore

log = logging.getLogger(__name__)

PASS_ITEMS = 100
PASS_SECONDS = 240.0
SETTLE = facet_job.SETTLE  # a session this quiet is read; one that is still going waits


def _todo(now: datetime, limit: int):
    """The local runs to read (see the module), the latest first: id, project, session and fingerprint."""
    r, e = tables.runs, tables.run_events
    live = facet_job._live_transcripts()
    told = exists().where(e.c.run_id == r.c.id, e.c.kind == "usage_update")
    return (
        select(r.c.id, r.c.project_id, r.c.session_id, live.c.fingerprint)
        .join_from(r, live, and_(live.c.project_id == r.c.project_id, live.c.session_id == r.c.session_id))
        .where(
            r.c.kind == "local",
            ~told,
            r.c.spend_transcripts.is_distinct_from(live.c.fingerprint),
            live.c.latest <= now - SETTLE,
        )
        .order_by(r.c.id.desc())
        .limit(limit)
    )


def _parts(project_id: int, session_id: str):
    """The parts of the session's transcript files: the subagents' first, then the session's own, each in order."""
    t, p = tables.session_transcripts, tables.transcript_parts
    return (
        select(t.c.file, p.c.seq, p.c.first_line, p.c.sha256.label("blob"), p.c.size)
        .join_from(t, p, p.c.transcript_id == t.c.id)
        .where(t.c.project_id == project_id, t.c.session_id == session_id, t.c.deleted_at.is_(None))
        .order_by(t.c.file == local.MAIN, t.c.file.collate("C"), p.c.seq)
    )


def bodies_of(store, session_id: str, files: dict[str, list], scratch: Path | None) -> list[dict]:
    """The usage_update bodies of a session's transcript ``files`` (file: its parts), as ``local_usage`` reads them."""
    reader = local_usage.UsageReader(session_id)
    for file, parts in files.items():
        key = "main" if file == local.MAIN else Path(file).name
        for _, line in facet_job.file_lines(store, parts, scratch):
            reader.line(key, line)
    return reader.done()


async def _read(engine: AsyncEngine, store, run, scratch: Path | None) -> None:
    async with engine.connect() as conn:
        rows = (await conn.execute(_parts(run.project_id, run.session_id))).all()
    files: dict[str, list] = {}
    for row in rows:
        files.setdefault(row.file, []).append(row)
    try:
        bodies = await asyncio.to_thread(bodies_of, store, run.session_id, files, scratch)
    except facet_job.Unavailable as exc:
        log.warning("local run spend not read", extra={"run_id": run.id, "error": str(exc)})
        bodies = None
    r = tables.runs
    async with engine.begin() as conn:
        if bodies is not None:
            reports = run_spend.reports_of("claude-code", [("usage_update", body) for body in bodies], "", "")
            await spend_store.write(conn, run.id, reports)
        await conn.execute(update(r).values(spend_transcripts=run.fingerprint).where(r.c.id == run.id))


async def local_spend(
    engine: AsyncEngine,
    store: BlobStore | None,
    *,
    scratch: Path | None = None,
    now: datetime | None = None,
    limit: int = PASS_ITEMS,
    seconds: float = PASS_SECONDS,
) -> dict:
    """One pass (see the module): how many local runs it read."""
    if store is None:
        return {}
    async with engine.connect() as conn:
        now = now or (await conn.execute(select(func.now()))).scalar_one()
        found = (await conn.execute(_todo(now, limit))).all()
    started, read = time.monotonic(), 0
    for run in found:
        if time.monotonic() - started > seconds:
            break
        await _read(engine, store, run, scratch)
        read += 1
    if read:
        log.info("local run spend read", extra={"runs": read, "found": len(found)})
    return {"read": read} if read else {}
