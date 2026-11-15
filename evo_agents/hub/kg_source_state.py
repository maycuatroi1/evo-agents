"""The state of one source of a project as the hub holds it, which ``kg sync`` reads instead of a corpus.sqlite (plan
kg-centralized-on-hub, step 3): GET /v1/kg/{project}/sources/{source}/state, and the same under /v1/worker/kg for the
worker on duty (``evo_agents.hub.server.kg``, ``kg_worker``).

From the runs of the source in kg_ingests and what each log says of it (``evo_agents.hub.kg_ingest.RunFacts``):

- ``last_run`` and ``last_ok``: the newest run, and the newest that ended ok, by run id (run ids are UUIDv7, so in the
  order the runs started), with when they started and ended; a run whose log does not say when it ended (its run_end
  has no ``finished_at``, as clients before this field write it) gives the time the hub received it. A sync asks ``last_ok`` whether a source is due and
  whether a git source's last run read the commit it would read now.
- ``cursor``: the cursor of the newest run that sent a state message, the one the next run of the source resumes from,
  as the build's merge keeps the newest (``Corpus.record_cursor``).
- ``lineage``: the lineage and epoch of the run whose (epoch, run id) is highest, as ``Corpus.adopt_lineage`` keeps
  it: the next run that reads from the same place takes that epoch, one that reads from elsewhere starts a new one.
- ``unread``: runs pushed before migration 0252 whose facts the next build has not filled in yet. While it is not 0,
  the three above may miss what those runs say; a caller that needs them asks for a build and reads again. A build
  reads every one of them: a run whose log it could not fetch or read is marked failed, so ``unread`` is 0 after the
  first build that succeeds (``fill_run_facts``).
- ``items``: the live items of the source in the latest build's corpus (kg_corpus_items of main), each id with its
  rev and rev_time, which a sync derives the deletions of a complete listing from. Runs pushed since that build are
  not in it yet; ``scope`` keeps the ids that start with it, and ``items=false`` leaves them out (``kg sync --due``
  reads only ``last_ok``). ``unbuilt``: how many runs of the source the hub received
  after the latest build of main started, which its items do not show yet (all of them when there is no build).
- ``held``: the removals the deletion guard held back (migration 0253), which ``kg sync --accept-removals`` applies:
  for each scope, those of the newest ok run that listed it completely (``kg_ingests.listed``). A run that lists a
  scope completely works its removals out again, so one that holds none of it clears that scope's hold, as the run of
  ``--accept-removals`` does for the scopes it applies; a run that does not list a scope leaves its hold as it is.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import Text, cast, func, select, true, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import tables
from evo_agents.hub.kg_graph_rows import MAIN
from evo_agents.hub.kg_ingest import LogProblem, RunFacts, run_facts

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


class Removal(BaseModel):
    run_id: str  # the run that held them back
    scope: str
    reason: str
    ids: list[str]


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
    unbuilt: int = 0
    held: list[Removal] = []


def _stamp(at: dt.datetime) -> str:
    """``at`` as ``evo_agents.kg.ids.utc_now`` writes a time."""
    return at.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


async def source_state(
    conn: AsyncConnection, project_id: int, source: str, scope: str | None = None, *, items: bool = True
) -> SourceState:
    """The state of ``source`` of project ``project_id`` (see the module); without ``items``, its items left out."""
    ingests = tables.kg_ingests
    run_id = cast(ingests.c.run_id, Text).label("run_id")
    of_source = (ingests.c.project_id == project_id, ingests.c.source == source)
    runs, unread = (
        await conn.execute(
            select(func.count(), func.count().filter(ingests.c.status.is_(None))).select_from(ingests).where(*of_source)
        )
    ).one()
    info = (run_id, ingests.c.status, ingests.c.run_started_at, ingests.c.run_finished_at, ingests.c.received_at)

    async def newest(*where, columns=info):
        query = select(*columns).where(*of_source, *where).order_by(ingests.c.run_id.desc()).limit(1)
        return (await conn.execute(query)).one_or_none()

    def run_info(row) -> RunInfo | None:
        if row is None:
            return None
        finished = row[3] or (_stamp(row[4]) if row[1] is not None else None)  # a log written before finished_at
        return RunInfo(run_id=row[0], status=row[1], started_at=row[2], finished_at=finished)

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
    held_back = await _held(conn, of_source)

    heads, builds = tables.kg_graph_heads, tables.kg_builds
    built = (
        select(builds.c.started_at)
        .join(heads, heads.c.build_id == builds.c.id)
        .where(heads.c.project_id == project_id, heads.c.branch_id == MAIN)
        .scalar_subquery()
    )
    unbuilt = (
        await conn.execute(
            select(func.count())
            .select_from(ingests)
            .where(*of_source, (built.is_(None)) | (ingests.c.received_at >= built))
        )
    ).scalar_one()

    rows = await _items(conn, project_id, source, scope) if items else []
    return SourceState(
        source=source,
        runs=runs,
        unread=unread,
        last_run=last_run,
        last_ok=last_ok,
        cursor=cursor,
        lineage=lineage,
        items=[Item(id=row[0], rev=row[1], rev_time=row[2]) for row in rows[:MAX_ITEMS]],
        items_cut=len(rows) > MAX_ITEMS,
        unbuilt=unbuilt,
        held=held_back,
    )


async def _items(conn: AsyncConnection, project_id: int, source: str, scope: str | None) -> list:
    """The id, rev and rev_time of the live items of ``source`` in the latest build of main, those that start with
    ``scope`` when given, in id order: MAX_ITEMS of them and one more, which says the list is cut."""
    corpus = tables.kg_corpus_items
    item_id = corpus.c.body["id"].astext
    query = select(item_id, corpus.c.body["rev"].astext, corpus.c.body["rev_time"].astext).where(
        corpus.c.project_id == project_id, corpus.c.branch_id == MAIN, corpus.c.source_id == source
    )
    if scope:
        query = query.where(func.starts_with(item_id, scope))
    return list((await conn.execute(query.order_by(item_id).limit(MAX_ITEMS + 1))).all())


async def _held(conn: AsyncConnection, of_source: tuple) -> list[Removal]:
    """The removals held back in each scope by the newest ok run that listed it completely."""
    ingests = tables.kg_ingests
    scope = func.jsonb_array_elements_text(ingests.c.listed).table_valued("value").lateral("scope")
    rows = await conn.execute(
        select(scope.c.value, cast(ingests.c.run_id, Text), ingests.c.held)
        .select_from(ingests.join(scope, true()))
        .where(*of_source, ingests.c.status == "ok", ingests.c.listed.is_not(None))
        .ext(distinct_on(scope.c.value))
        .order_by(scope.c.value, ingests.c.run_id.desc())
    )
    return [
        Removal(run_id=run, **removal) for name, run, held in rows for removal in held or () if removal["scope"] == name
    ]


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


UNREAD_LOG = RunFacts().columns()  # status failed and nothing else: what a run whose log no build can read gets


async def fill_run_facts(engine: AsyncEngine, logs: Path, project_id: int) -> int:
    """Fill in the facts of the runs of project ``project_id`` pushed before migration 0252 (``kg_ingests.status``
    null) from their logs under ``logs``, the build's cache, which has just fetched them, and mark failed those whose
    log is not there or does not read; how many runs it read (``evo_agents.hub.kg_build.run_build``)."""
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
        for run_id in run_ids:
            columns = found.get(run_id, UNREAD_LOG)
            await conn.execute(
                update(ingests)
                .where(
                    ingests.c.project_id == project_id,
                    ingests.c.run_id == uuid.UUID(run_id),
                    ingests.c.status.is_(None),
                )
                .values(**columns)
            )
    return len(run_ids)
