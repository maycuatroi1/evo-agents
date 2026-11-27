"""The run that writes a project's starter questions before its kg_init run measures them
(``evo_agents.hub.server.kg_questions`` has the route, the graph reads and the plan): the phase of the kg_init run, and
keeping the questions sent as the next version of the project's eval set. ``evo_agents.hub.kg_questions`` holds the
rules without the database."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import kg_questions as model
from evo_agents.hub import runs
from evo_agents.hub.kg_eval_starter import set_digest
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.kg import built_graphs
from evo_agents.hub.server.kg.models.questions import Count, QuestionsOut, Queue
from evo_agents.hub.server.kg.repository import questions as repository
from evo_agents.hub.server.kg_audience import agent_audience
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import Principal, holds
from evo_agents.hub.tables_kg import INIT_ACTIVE
from evo_agents.kg.evaluate import HOLDOUT, split_of

log = logging.getLogger("evo_agents.hub.server.kg_questions")  # the name its log lines had before the layers

OwnerReads = Callable[..., Awaitable[tuple]]  # kg_health._owner_reads


# Reading the graph as the agents' sink does


async def owner_view(engine: AsyncEngine, row, graphs_tried: int, owner_reads: OwnerReads) -> tuple:
    """(the latest ``graphs_tried`` graphs of ``row``'s project, its owner's access, the agents' sink, the owner's
    audience, None), or (graphs, None, None, None, why the graph cannot be read so), in a transaction of its own;
    ``owner_reads`` is kg_health's."""
    async with UnitOfWork(engine) as conn:
        graphs = await built_graphs(conn, row.project_id, graphs_tried)
        access, sink, why = await owner_reads(conn, row, graphs, agent=True)
        if why is not None:
            return graphs, None, None, None, why
        audience = await agent_audience(conn, row.owner_id, None, None)
    return graphs, access, sink, audience, None


# The phase of the kg_init run


async def _needed(conn: AsyncConnection, project_id: int) -> str | None:
    row = await repository.latest_set(conn, project_id)
    if row is None:
        return model.needs_questions(None, 0, False)
    return model.needs_questions(row.name, int(row.n or 0), row.created_by is None)


def _skipped(body: dict, asked: dict, why: str) -> tuple[bool, dict]:
    body["questions"] = {**asked, "state": model.SKIPPED, "why": why}
    body.setdefault("notes", []).append(f"no starter questions: {why}")
    return False, body


async def _first(engine: AsyncEngine, ctx, body: dict, reader, now: datetime, blobs, data_dir, count: Count):
    """Whether the project needs a questions run and what its sink reads: body["questions"] as asked, or ended."""
    async with UnitOfWork(engine) as conn:
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
    async with UnitOfWork(engine) as conn:
        run_id = await queue(conn, ctx, body, None, lens=model.LENS)
        if run_id is None:
            if datetime.fromisoformat(asked["asked_at"]) + model.WAIT < now:
                notes = [n for n in body.get("notes") or [] if n.startswith("no review run")]
                waits, body = _skipped(
                    body, asked, f"no review run could be queued in {model.WAIT}: {'; '.join(notes[-1:])}"
                )
            else:
                waits = True
            await repository.save(conn, ctx.id, body)
            return waits, body
        asked |= {"state": model.RUNNING, "agent_run_id": run_id, "queued_at": now.isoformat()}
        await repository.save(conn, ctx.id, body, questions_run_id=run_id)
    log.info("kg starter questions run queued", extra={"project": ctx.project, "init_run_id": ctx.id, "run_id": run_id})
    return True, body


async def _running(engine: AsyncEngine, ctx, body: dict, now: datetime) -> tuple[bool, dict]:
    asked = body["questions"]
    async with UnitOfWork(engine) as conn:
        state = await repository.run_state(conn, asked.get("agent_run_id"))
        if state in runs.ACTIVE_STATES:
            if datetime.fromisoformat(asked["queued_at"]) + model.WAIT >= now:
                return True, body
            asked |= {"state": model.TIMED_OUT, "ended": f"still {state} after {model.WAIT}: measured without it"}
        elif asked.get("version"):
            asked |= {"state": model.DONE, "ended": f"the run ended {state or 'gone'}"}
        else:
            asked |= {"state": model.FAILED, "ended": f"the run ended {state or 'gone'} without a question set"}
            body.setdefault("notes", []).append(f"no starter questions: {asked['ended']}")
        await repository.save(conn, ctx.id, body)
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
    count: Count,
    queue: Queue,
) -> tuple[bool, dict]:
    """(whether kg_init run ``ctx`` waits for its questions run, its body). ``reader`` is the row its graph is read as
    (project_id, project, owner_id, owner), None without one; ``count`` counts what the agents' sink reads of the
    graph (``kg_questions.count_readable``); ``queue`` queues a review run (``kg_init._queue_agent``)."""
    asked = body.get("questions") or {}
    if asked.get("state") in model.ENDED:
        return False, body
    if not asked:
        body = await _first(engine, ctx, body, reader, now, blobs, data_dir, count)
        asked = body["questions"]
        if asked["state"] in model.ENDED:
            async with UnitOfWork(engine) as conn:
                await repository.save(conn, ctx.id, body)
            return False, body
    if asked["state"] == model.QUEUED:
        return await _queue(engine, ctx, body, now, queue)
    return await _running(engine, ctx, body, now)


def shown(body: dict) -> dict | None:
    """body["questions"] as ``hub kg init --status`` shows it: without the prompt."""
    asked = body.get("questions") if isinstance(body, dict) else None
    return {k: v for k, v in asked.items() if k != "prompt"} if isinstance(asked, dict) else None


# Keeping the questions sent


async def _init_row(conn: AsyncConnection, user: Principal, access, run_id: int):
    row = await repository.init_run(conn, access.project_id, run_id)
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


async def waiting_run(engine: AsyncEngine, user: Principal, project: str, run_id: int) -> tuple:
    """(the access of ``user`` to the project, its kg_init run ``run_id``) when the user is a writer and the run waits
    for the starter questions of its questions run, in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        if not holds(access.role, "writer"):
            raise HTTPException(
                403, f"sending the starter questions of project {access.name} needs the writer role on it"
            )
        return access, await _init_row(conn, user, access, run_id)


async def keep(
    engine: AsyncEngine, user: Principal, access, run_id: int, entries: list[dict], classes: dict
) -> QuestionsOut:
    """Keep the checked ``entries`` as the next version of the project's eval set, while kg_init run ``run_id`` still
    waits for them, in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        row = await _init_row(conn, user, access, run_id)
        await repository.lock_project(conn, access.project_id)
        version = (await repository.latest_version(conn, access.project_id) or 0) + 1
        await repository.add_set(
            conn, access.project_id, version, model.SET_NAME, entries, set_digest(entries), user.user_id
        )
        body = dict(row.body or {})
        body["questions"] = {**body["questions"], "version": version, "count": len(entries), "classes": classes}
        await repository.save(conn, row.id, body)
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
