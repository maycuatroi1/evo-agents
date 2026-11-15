"""The state of one source of a project as the hub holds it, which ``kg sync`` reads instead of a corpus.sqlite (plan
kg-centralized-on-hub, step 3): GET /v1/kg/{project}/sources/{source}/state, and the same under /v1/worker/kg for the
worker on duty (``evo_agents.hub.server.kg``, ``kg_worker``).

From the runs of the source in kg_ingests and what each log says of it (``evo_agents.hub.kg_ingest.RunFacts``):

- ``last_run`` and ``last_ok``: the newest run, and the newest that ended ok, by run id (run ids are UUIDv7, so in the
  order the runs started), with when they started and ended. A sync asks ``last_ok`` whether a source is due and
  whether a git source's last run read the commit it would read now.
- ``cursor``: the cursor of the newest run that sent a state message, the one the next run of the source resumes from,
  as the build's merge keeps the newest (``Corpus.record_cursor``).
- ``lineage``: the lineage and epoch of the run whose (epoch, run id) is highest, as ``Corpus.adopt_lineage`` keeps
  it: the next run that reads from the same place takes that epoch, one that reads from elsewhere starts a new one.
- ``unread``: runs pushed before migration 0252 whose facts the next build has not filled in yet. While it is not 0,
  the three above may miss what those runs say; a caller that needs them asks for a build and reads again.
- ``items``: the live items of the source in the latest build's corpus (kg_corpus_items of main), each id with its
  rev and rev_time, which a sync derives the deletions of a complete listing from. Runs pushed since that build are
  not in it yet; ``scope`` keeps the ids that start with it.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import Text, cast, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import tables
from evo_agents.hub.kg_graph_rows import MAIN
from evo_agents.hub.kg_ingest import LogProblem, run_facts

MAX_ITEMS = 200_000


class RunInfo(BaseModel):
    run_id: str
    status: str | None
    started_at: str | None
    finished_at: str | None


class Cursor(BaseModel):
    run_id: str
    cursor: object = None


class Lineage(BaseModel):
    run_id: str
    lineage: str
    epoch: str


class Item(BaseModel):
    id: str
    rev: str | None
    rev_time: str | None


class SourceState(BaseModel):
    source: str
    runs: int
    unread: int
    last_run: RunInfo | None
    last_ok: RunInfo | None
    cursor: Cursor | None
    lineage: Lineage | None
    items: list[Item]
    items_cut: bool = False  # more than MAX_ITEMS items: only the first are listed


async def source_state(conn: AsyncConnection, project_id: int, source: str, scope: str | None = None) -> SourceState:
    """The state of ``source`` of project ``project_id`` (see the module)."""
    ingests = tables.kg_ingests
    run_id = cast(ingests.c.run_id, Text).label("run_id")
    of_source = (ingests.c.project_id == project_id, ingests.c.source == source)
    runs, unread = (
        await conn.execute(
            select(func.count(), func.count().filter(ingests.c.status.is_(None))).select_from(ingests).where(*of_source)
        )
    ).one()
    info = (run_id, ingests.c.status, ingests.c.run_started_at, ingests.c.run_finished_at)

    async def newest(*where, columns=info):
        query = select(*columns).where(*of_source, *where).order_by(ingests.c.run_id.desc()).limit(1)
        return (await conn.execute(query)).one_or_none()

    def run_info(row) -> RunInfo | None:
        return RunInfo(run_id=row[0], status=row[1], started_at=row[2], finished_at=row[3]) if row else None

    last_run = run_info(await newest())
    last_ok = run_info(await newest(ingests.c.status == "ok"))
    held = await newest(ingests.c.cursor.is_not(None), columns=(run_id, ingests.c.cursor))
    cursor = Cursor(run_id=held[0], cursor=held[1].get("cursor")) if held else None
    found = (
        await conn.execute(
            select(run_id, ingests.c.lineage, ingests.c.epoch)
            .where(*of_source, ingests.c.lineage.is_not(None), ingests.c.epoch.is_not(None))
            .order_by(ingests.c.epoch.desc(), ingests.c.run_id.desc())
            .limit(1)
        )
    ).one_or_none()
    lineage = Lineage(run_id=found[0], lineage=found[1], epoch=found[2]) if found else None

    corpus = tables.kg_corpus_items
    item_id = corpus.c.body["id"].astext
    query = select(item_id, corpus.c.body["rev"].astext, corpus.c.body["rev_time"].astext).where(
        corpus.c.project_id == project_id, corpus.c.branch_id == MAIN, corpus.c.source_id == source
    )
    if scope:
        query = query.where(func.starts_with(item_id, scope))
    rows = (await conn.execute(query.order_by(item_id).limit(MAX_ITEMS + 1))).all()
    items = [Item(id=row[0], rev=row[1], rev_time=row[2]) for row in rows[:MAX_ITEMS]]
    return SourceState(
        source=source,
        runs=runs,
        unread=unread,
        last_run=last_run,
        last_ok=last_ok,
        cursor=cursor,
        lineage=lineage,
        items=items,
        items_cut=len(rows) > MAX_ITEMS,
    )


def _facts_of(logs: Path, run_ids: list[str]) -> dict[str, dict]:
    """The kg_ingests columns of each run of ``run_ids`` whose log is under ``logs`` and reads."""
    found = {}
    for run_id in run_ids:
        path = logs / f"{run_id}.jsonl.gz"
        if not path.is_file():
            continue
        try:
            found[run_id] = run_facts(path).columns()
        except LogProblem:
            continue
    return found


async def fill_run_facts(engine: AsyncEngine, logs: Path, project_id: int) -> int:
    """Fill in the facts of the runs of project ``project_id`` pushed before migration 0252 (``kg_ingests.status``
    null) from their logs under ``logs``, the build's cache, which has just fetched them; how many runs were filled
    in (``evo_agents.hub.kg_build.run_build``)."""
    ingests = tables.kg_ingests
    async with engine.connect() as conn:
        rows = await conn.execute(
            select(cast(ingests.c.run_id, Text)).where(ingests.c.project_id == project_id, ingests.c.status.is_(None))
        )
        run_ids = list(rows.scalars())
    if not run_ids:
        return 0
    found = await asyncio.to_thread(_facts_of, logs, run_ids)
    async with engine.begin() as conn:
        for run_id, columns in found.items():
            await conn.execute(
                update(ingests)
                .where(
                    ingests.c.project_id == project_id,
                    ingests.c.run_id == uuid.UUID(run_id),
                    ingests.c.status.is_(None),
                )
                .values(**columns)
            )
    return len(found)
