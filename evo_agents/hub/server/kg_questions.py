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
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub import kg_questions as model
from evo_agents.hub import kg_snapshots
from evo_agents.hub.kg_graph import GraphUnavailable, reading
from evo_agents.hub.server import kg_health
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg.models.questions import Count, QuestionsBody, QuestionsOut, Queue
from evo_agents.hub.server.kg.service import questions as service
from evo_agents.hub.server.kg.service.questions import shown as shown  # kg_init's, for hub kg init --status
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/kg", tags=["kg"], responses={401: {"model": ErrorBody}})


# Reading the graph as the agents' sink does


async def _read_graph(engine: AsyncEngine, row, blobs, data_dir: Path | None, read: Callable) -> dict:
    """``read(store, ceiling, sink)`` on the latest graph of ``row``'s project (project_id, project, owner_id, owner),
    as its owner's agents read it, in a thread; {"why": ...} when it cannot."""
    graphs, access, sink, audience, why = await service.owner_view(
        engine, row, kg_health.GRAPHS_TRIED, kg_health._owner_reads
    )
    if why is not None:
        return {"why": why}
    label = access.rules.ceiling(access.max_level, sink, audience)
    ceiling = (label.level, label.location) if label else None

    def blocking(loaded) -> dict:
        snapshot = kg_health.snapshot_of(loaded, graphs, None)
        with reading(row.project, snapshot) as (store, _):
            store.reader = audience
            return {"sink": sink, **read(store, ceiling)}

    try:
        loaded = await kg_snapshots.load(engine, row.project_id)
        return await asyncio.to_thread(blocking, loaded)
    except GraphUnavailable as exc:
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
    return await service.phase(
        engine, ctx, body, reader, now, blobs=blobs, data_dir=data_dir, count=count or count_readable, queue=queue
    )


# The route


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
    access, row = await service.waiting_run(engine, user, project, run_id)
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
    out = await service.keep(engine, user, access, run_id, checked["entries"], checked["classes"])
    log.info(
        "kg starter questions kept",
        extra={"project": access.name, "init_run_id": run_id, "version": out.version, "questions": out.questions},
    )
    return out
