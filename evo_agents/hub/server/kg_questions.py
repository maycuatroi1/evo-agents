"""The run that writes a project's starter questions before its kg_init run measures them (plan kg-upload-archive,
criterion a6); ``evo_agents.hub.kg_questions`` holds the rules without the database.

- ``phase``, called by a kg_init run still ``queued`` (``evo_agents.hub.server.kg_init``): when the project needs
  questions (``kg_questions.needs_questions``), count what the agents' sink reads of the latest graph; with a document
  to read, queue a review run with lens kg_questions on the project's default agent pool under the kg_init run's caps,
  and wait for it to end, or for ``kg_questions.WAIT``; with none, skip it and say why in the kg_init run's notes. What
  the questions run was asked and did is ``body["questions"]`` of the kg_init run, which ``hub kg init --status``
  shows; ``questions_run_id`` names the review run.
- POST /v1/kg/{project}/init/{run_id}/questions (``evo-agents hub kg init --questions FILE``): the agent of the
  questions run, or a writer, sends the questions. The hub checks every answer against the graph as the agents' sink
  reads it (``kg_questions.check``) and keeps them as the next version of the project's eval set; 422 with the problems,
  none of which names an item, otherwise.

Logs name the project and the runs, and count; never an item, a path or a question.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import kg_questions as model
from evo_agents.hub import runs, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.kg_build import kg_root
from evo_agents.hub.kg_eval_starter import set_digest
from evo_agents.hub.kg_graph import ArtifactMismatch, GraphCache, GraphUnavailable, open_graph
from evo_agents.hub.server import audit, kg_eval_sets, kg_health
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg import built_graphs
from evo_agents.hub.server.kg_audience import agent_audience
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import CurrentUser, Principal
from evo_agents.hub.tables_kg import INIT_ACTIVE
from evo_agents.kg.evaluate import HOLDOUT, split_of

log = logging.getLogger(__name__)

CACHE = "questions"  # the directory of the worker's cache (``kg_root``) the graphs read here are fetched into
MAX_QUESTIONS = 2000

router = APIRouter(prefix="/v1/kg", tags=["kg"], responses={401: {"model": ErrorBody}})

Count = Callable[[AsyncEngine, object, object, "Path | None"], Awaitable[dict]]
Queue = Callable[..., Awaitable["int | None"]]


# Reading the graph as the agents' sink does


async def _read_graph(engine: AsyncEngine, row, blobs, data_dir: Path | None, read: Callable) -> dict:
    """``read(store, ceiling, sink)`` on the latest graph of ``row``'s project (project_id, project, owner_id, owner),
    as its owner's agents read it, in a thread; {"why": ...} when it cannot."""
    async with engine.begin() as conn:
        graphs = await built_graphs(conn, row.project_id, kg_health.GRAPHS_TRIED)
        access, sink, why = await kg_health._owner_reads(conn, row, blobs, data_dir, graphs, agent=True)
        if why is not None:
            return {"why": why}
        audience = await agent_audience(conn, row.owner_id, None, None)
    label = access.rules.ceiling(access.max_level, sink, audience)
    ceiling = (label.level, label.location) if label else None
    cache = GraphCache(kg_root(data_dir) / CACHE, keep=1)

    def blocking() -> dict:
        path, graph = cache.acquire(blobs, row.project, graphs)
        try:
            store = open_graph(path, row.project, graph)
            try:
                store.reader = audience
                return {"sink": sink, **read(store, ceiling)}
            finally:
                store.close()
        finally:
            cache.release(path)

    try:
        return await asyncio.to_thread(blocking)
    except (GraphUnavailable, ArtifactMismatch) as exc:
        return {"why": str(exc)}
    except Exception as exc:
        log.exception("reading the graph for starter questions failed", extra={"project": row.project})
        return {"why": f"the graph could not be read: {type(exc).__name__}"}


async def count_readable(engine: AsyncEngine, row, blobs, data_dir: Path | None) -> dict:
    """What the agents' sink reads of the project's latest graph (``kg_questions.Readable.to_json`` and the sink), or
    {"why": ...}."""
    return await _read_graph(
        engine, row, blobs, data_dir, lambda store, ceiling: model.readable(store, ceiling).to_json()
    )


async def check_questions(engine: AsyncEngine, row, blobs, data_dir: Path | None, questions: list, found: dict) -> dict:
    """``kg_questions.check`` of ``questions`` against the project's latest graph as the agents' sink reads it:
    {"entries", "classes", "problems"}, or {"why": ...}."""
    readable = model.Readable(int(found.get("documents") or 0), 0, dict(found.get("classes") or {}))

    def read(store, ceiling) -> dict:
        checked = model.check(store, ceiling, questions, readable)
        return {"entries": checked.entries, "classes": checked.classes, "problems": checked.problems}

    return await _read_graph(engine, row, blobs, data_dir, read)


# The phase of the kg_init run


async def _save(conn: AsyncConnection, ctx, body: dict, **values) -> None:
    t = tables.kg_init_runs
    await conn.execute(update(t).where(t.c.id == ctx.id).values(body=body, updated_at=func.now(), **values))


async def _needed(conn: AsyncConnection, project_id: int) -> str | None:
    s = tables.kg_eval_sets
    row = (
        await conn.execute(
            select(s.c.name, func.jsonb_array_length(s.c.questions).label("n"), s.c.created_by)
            .where(s.c.project_id == project_id)
            .order_by(s.c.version.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return model.needs_questions(None, 0, False)
    return model.needs_questions(row.name, int(row.n or 0), row.created_by is None)


def _skipped(body: dict, asked: dict, why: str) -> tuple[bool, dict]:
    body["questions"] = {**asked, "state": model.SKIPPED, "why": why}
    body.setdefault("notes", []).append(f"no starter questions: {why}")
    return False, body


async def _first(engine: AsyncEngine, ctx, body: dict, reader, now: datetime, blobs, data_dir, count: Count):
    """Whether the project needs a questions run and what its sink reads: body["questions"] as asked, or ended."""
    async with engine.begin() as conn:
        why = await _needed(conn, ctx.project_id)
    if why is None:
        body["questions"] = {"state": model.NOT_NEEDED, "why": "the project's eval set has enough questions"}
        return body
    if reader is None:
        return _skipped(body, {}, "the project has no admin to read the graph as")[1]
    found = await count(engine, reader, blobs, data_dir)
    if found.get("why"):
        return _skipped(body, {}, f"the graph could not be read: {found['why']}")[1]
    asked = {
        "why": why,
        "sink": found.get("sink"),
        "readable": {k: found.get(k) for k in ("documents", "above", "classes")},
    }
    if not found.get("documents"):
        above = int(found.get("above") or 0)
        return _skipped(
            body, asked, f"the sink {found.get('sink')} of the agents clears none of the {above} documents of the graph"
        )[1]
    asked |= {"state": model.QUEUED, "target": model.target(int(found["documents"])), "asked_at": now.isoformat()}
    asked["prompt"] = model.prompt(
        project=ctx.project,
        init_id=ctx.id,
        sink=str(found.get("sink")),
        found=model.Readable(int(found["documents"]), int(found.get("above") or 0), dict(found.get("classes") or {})),
        why=why,
    )
    body["questions"] = asked
    return body


async def _queue(engine: AsyncEngine, ctx, body: dict, now: datetime, queue: Queue) -> tuple[bool, dict]:
    asked = body["questions"]
    async with engine.begin() as conn:
        run_id = await queue(conn, ctx, body, None, lens=model.LENS)
        if run_id is None:
            if datetime.fromisoformat(asked["asked_at"]) + model.WAIT < now:
                notes = [n for n in body.get("notes") or [] if n.startswith("no review run")]
                waits, body = _skipped(
                    body, asked, f"no review run could be queued in {model.WAIT}: {'; '.join(notes[-1:])}"
                )
            else:
                waits = True
            await _save(conn, ctx, body)
            return waits, body
        asked |= {"state": model.RUNNING, "agent_run_id": run_id, "queued_at": now.isoformat()}
        await _save(conn, ctx, body, questions_run_id=run_id)
    log.info("kg starter questions run queued", extra={"project": ctx.project, "init_run_id": ctx.id, "run_id": run_id})
    return True, body


async def _running(engine: AsyncEngine, ctx, body: dict, now: datetime) -> tuple[bool, dict]:
    asked = body["questions"]
    r = tables.runs
    async with engine.begin() as conn:
        state = await conn.scalar(select(r.c.state).where(r.c.id == asked.get("agent_run_id")))
        if state in runs.ACTIVE_STATES:
            if datetime.fromisoformat(asked["queued_at"]) + model.WAIT >= now:
                return True, body
            asked |= {"state": model.TIMED_OUT, "ended": f"still {state} after {model.WAIT}: measured without it"}
        elif asked.get("version"):
            asked |= {"state": model.DONE, "ended": f"the run ended {state or 'gone'}"}
        else:
            asked |= {"state": model.FAILED, "ended": f"the run ended {state or 'gone'} without a question set"}
            body.setdefault("notes", []).append(f"no starter questions: {asked['ended']}")
        await _save(conn, ctx, body)
    log.info(
        "kg starter questions run ended", extra={"project": ctx.project, "init_run_id": ctx.id, "state": asked["state"]}
    )
    return False, body


async def phase(
    engine: AsyncEngine,
    ctx,
    body: dict,
    reader,
    now: datetime,
    *,
    blobs=None,
    data_dir=None,
    count: Count | None = None,
    queue: Queue,
) -> tuple[bool, dict]:
    """(whether kg_init run ``ctx`` waits for its questions run, its body). ``reader`` is the row its graph is read as
    (project_id, project, owner_id, owner), None without one; ``queue`` queues a review run (``kg_init._queue_agent``)."""
    asked = body.get("questions") or {}
    if asked.get("state") in model.ENDED:
        return False, body
    if not asked:
        body = await _first(engine, ctx, body, reader, now, blobs, data_dir, count or count_readable)
        asked = body["questions"]
        if asked["state"] in model.ENDED:
            async with engine.begin() as conn:
                await _save(conn, ctx, body)
            return False, body
    if asked["state"] == model.QUEUED:
        return await _queue(engine, ctx, body, now, queue)
    return await _running(engine, ctx, body, now)


def shown(body: dict) -> dict | None:
    """body["questions"] as ``hub kg init --status`` shows it: without the prompt."""
    asked = body.get("questions") if isinstance(body, dict) else None
    return {k: v for k, v in asked.items() if k != "prompt"} if isinstance(asked, dict) else None


# The route


class QuestionsBody(BaseModel):
    questions: list[dict] = Field(
        min_length=1,
        max_length=MAX_QUESTIONS,
        description="the questions: each {question, files and/or nodes}, an id optional",
    )


class QuestionsOut(BaseModel):
    run: int = Field(description="the kg_init run")
    set: str
    version: int = Field(description="the version of the project's eval set the questions are")
    questions: int
    holdout: int = Field(description="the questions in the holdout part, which the kg_init run does not see")
    classes: dict[str, int] = Field(description="questions by the class of data their answers are of")


async def _init_row(conn: AsyncConnection, user: Principal, access, run_id: int):
    t = tables.kg_init_runs
    row = (
        await conn.execute(select(t).where(t.c.id == run_id, t.c.project_id == access.project_id).with_for_update())
    ).first()
    if row is None:
        raise HTTPException(404, f"project {access.name} has no kg_init run {run_id}")
    asked = (row.body or {}).get("questions") or {}
    if row.state not in INIT_ACTIVE or asked.get("state") not in (model.RUNNING, model.TIMED_OUT):
        raise HTTPException(409, f"kg_init run {run_id} is not waiting for starter questions")
    if user.acts_for_run is not None and user.acts_for_run != asked.get("agent_run_id"):
        raise HTTPException(403, f"run {user.acts_for_run} is not the questions run of kg_init run {run_id}")
    if asked.get("version"):
        raise HTTPException(409, f"kg_init run {run_id} has its starter questions already")
    return row


async def _keep(
    conn: AsyncConnection, user: Principal, access, row, entries: list[dict], classes: dict
) -> QuestionsOut:
    s = tables.kg_eval_sets
    await conn.execute(select(tables.projects.c.id).where(tables.projects.c.id == access.project_id).with_for_update())
    latest = await kg_eval_sets.latest_row(conn, access.project_id)
    version = (latest.version if latest else 0) + 1
    await conn.execute(
        s.insert().values(
            project_id=access.project_id,
            version=version,
            name=model.SET_NAME,
            questions=entries,
            digest=set_digest(entries),
            created_by=user.user_id,
        )
    )
    body = dict(row.body or {})
    body["questions"] = {**body["questions"], "version": version, "count": len(entries), "classes": classes}
    t = tables.kg_init_runs
    await conn.execute(update(t).where(t.c.id == row.id).values(body=body, updated_at=func.now()))
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.KG_EVAL_SET,
        target=f"{access.name} set={model.SET_NAME} version={version} questions={len(entries)} init={row.id}",
        project_id=access.project_id,
    )
    holdout = sum(split_of(str(e["id"])) == HOLDOUT for e in entries)
    return QuestionsOut(
        run=row.id, set=model.SET_NAME, version=version, questions=len(entries), holdout=holdout, classes=classes
    )


@router.post(
    "/{project}/init/{run_id}/questions",
    response_model=QuestionsOut,
    status_code=201,
    responses={code: {"model": ErrorBody} for code in (403, 404, 409, 422)},
)
async def put_questions(
    request: Request, project: ProjectName, run_id: int, body: QuestionsBody, user: CurrentUser
) -> QuestionsOut:
    """Send the starter questions of a kg_init run: kept as the next version of the project's eval set when every
    answer is a file or a node of the graph the agents' sink reads, and they are enough and cover its data."""
    engine = request.app.state.engine
    async with engine.begin() as conn:
        access = await project_access(conn, user, project)
        if not has_role(access.role, "writer"):
            raise HTTPException(
                403, f"sending the starter questions of project {access.name} needs the writer role on it"
            )
        row = await _init_row(conn, user, access, run_id)
    reader = SimpleNamespace(project_id=access.project_id, project=access.name, owner_id=user.user_id, owner=user.login)
    found = (row.body or {})["questions"].get("readable") or {}
    checked = await check_questions(
        engine, reader, request.app.state.blobs, request.app.state.config.data_dir, body.questions, found
    )
    if checked.get("why"):
        raise HTTPException(409, f"the graph of project {access.name} cannot be read: {checked['why']}")
    if checked["problems"]:
        log.info(
            "kg starter questions refused",
            extra={"project": access.name, "init_run_id": run_id, "problems": len(checked["problems"])},
        )
        raise HTTPException(422, "the questions were not kept: " + "; ".join(checked["problems"]))
    async with engine.begin() as conn:
        row = await _init_row(conn, user, access, run_id)
        out = await _keep(conn, user, access, row, checked["entries"], checked["classes"])
    log.info(
        "kg starter questions kept",
        extra={"project": access.name, "init_run_id": run_id, "version": out.version, "questions": out.questions},
    )
    return out
