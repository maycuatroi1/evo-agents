"""The kg_init run of a project (plan kg-data-profile-curator, criterion a5): the Curator configures the project's
knowledge graph from its own data profile, without waiting for a person. A run is a row of kg_init_runs; the job
curator.collect moves it on each minute (``advance``):

- ``queued``: asked for by the first successful build of a project with a knowledge config, by a profile that changed a
  lot since the last run's (``evo_agents.hub.kg_init_db.on_build``), or by a call (POST /v1/kg/{project}/init,
  ``evo-agents hub kg init --project P``; POST /v1/admin/kg-init for every project, an admin's). A project with no eval
  set, or only its commits' set with under 30 questions, first gets a review run with lens kg_questions that writes the
  starter questions, waited for until it ends or for two hours (``evo_agents.hub.server.kg_questions``). The hub measures the
  starter questions (the tune split of the project's eval set, by words and by meaning when the graph has vectors),
  writes the prompt (the profile, the build's coverage, the strategies, the measures, the optimize loop's last figures)
  and queues a review run with lens kg_init on the project's default agent pool, under the charter's review cost cap
  (``kg_init.HUB_CAP_USD`` without a charter). No night shift is needed. The run stays queued, and says why in
  ``body["notes"]``, while no node of the pool fits it or another review run of the project is active.
- ``running``: the agent decides and applies. It records its decision with POST /v1/kg/{project}/init/{id}/decision
  (``evo-agents hub kg init --decide FILE``): the hub validates it (``evo_agents.kg.init_run.parse_decision``: each
  part cites figures), sets the strategy and the settings with their reasons and figures in the settings' history, and
  queues a rebuild when a setting needs one. Sources and changes to the KG code, a dependency or the worker's runtime go
  through the proposal flow (kg_propose); the decision keeps them with their reasons.
- ``done`` once the review run ended with a decision, ``failed`` once it ended without one.

The prompt holds counts, shapes and figures, never the text or the name of an item: what a sink may not see cannot
reach a prompt. One run of a project is in flight at a time: a second call returns the first. Limits on the hub: the
candidate strategies are not measured here (the figures are those of the current settings), a strategy the Curator
creates sets its settings and keeps its identifiers, ontology and steps as pending for the proposal flow, and the
``strategy`` setting is set to built-in strategies only, as the worker that builds cannot read a file created here.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub import kg_health_eval, kg_snapshots
from evo_agents.hub.kg_graph import GraphUnavailable, HubSession, reading
from evo_agents.hub.server import kg_eval_sets, kg_health, kg_questions
from evo_agents.hub.server.admin import admin
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg.models.init import (
    MAX_LISTED,
    MAX_TUNE,
    DecisionBody,
    DecisionOut,
    InitAll,
    InitRun,
    InitRuns,
    Measure,
)
from evo_agents.hub.server.kg.repository.init import asker_of as asker_of  # kg_answers and kg_entity_map_run's
from evo_agents.hub.server.kg.service import init as service
from evo_agents.hub.server.kg.service.init import queue_repoless as queue_repoless  # kg_answers and kg_entity_map_run's
from evo_agents.hub.server.security import CurrentUser
from evo_agents.kg import init_run
from evo_agents.kg.evaluate import TUNE

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/kg", tags=["kg"], responses={401: {"model": ErrorBody}})
admin_router = APIRouter(
    prefix="/v1/admin", tags=["admin"], dependencies=[Depends(admin)], responses={401: {"model": ErrorBody}}
)


# Measuring


def _starter(project: str, access, sink: str, graphs, found_set, audience, loaded=None) -> dict:
    """The tune split of ``found_set`` asked of the latest graph by words and by meaning, read with ``audience``: of
    ``loaded``, the snapshot of the hub's tables, when it is of one of ``graphs``, else of the graph file. Blocking:
    run it in a thread. {"why": ...} when it cannot; the holdout is never asked."""
    question_set = found_set[0] if found_set else None
    questions = [q for q in (question_set.part(TUNE) if question_set else []) if q.project in (None, project)]
    if not questions:
        return {"why": "the project's eval set has no tune question"}
    questions = questions[:MAX_TUNE]
    ceiling = access.rules.ceiling(access.max_level, sink, audience)
    with reading(project, kg_health.snapshot_of(loaded, graphs, ceiling)) as (store, graph):
        if store.latest_ready() is None:
            return {"why": f"build {graph.build_id} holds no ready graph"}
        session = HubSession(project, access.rules.policy, ceiling, sink, store, graph, graphs[0])

        def asked(*, meaning: bool):
            session.semantic = meaning
            found = kg_health_eval.score(questions, [], 0, session, plan_steps=0, sink=sink)
            return init_run.figures((found.get("methods") or {}).get("kg_search"))

        found = {"build_id": graph.build_id, "words": asked(meaning=False), "meaning": None}
        if store.vector_info() is not None:
            try:
                found["meaning"] = asked(meaning=True)
            except Exception as exc:  # no model here: the figures by words stand
                found["why_meaning"] = f"{type(exc).__name__}: {exc}"[:200]
        return found


async def measure_starter(engine: AsyncEngine, row, blobs, data_dir: Path | None) -> dict:
    """The starter measure of ``row``'s project (project_id, project, owner_id, owner), as its owner's agents read the
    graph; {"why": ...} when it cannot."""
    graphs, access, sink, found_set, audience, why = await service.starter_view(
        engine, row, kg_health.GRAPHS_TRIED, kg_health._owner_reads, kg_eval_sets.latest_set
    )
    if why is not None:
        return {"why": why}
    try:
        loaded = await kg_snapshots.load(engine, row.project_id)
        return await asyncio.to_thread(_starter, row.project, access, sink, graphs, found_set, audience, loaded=loaded)
    except GraphUnavailable as exc:
        return {"why": str(exc)}
    except Exception as exc:  # a run goes on without figures
        log.exception("the starter measure of a kg init run failed", extra={"project": row.project})
        return {"why": f"the measure failed: {type(exc).__name__}"}


# The job


async def advance(
    engine: AsyncEngine,
    *,
    blobs=None,
    data_dir=None,
    now: datetime | None = None,
    measure: Measure | None = None,
    count: kg_questions.Count | None = None,
) -> dict:
    """Move each kg_init run in flight one phase on (see the module), each on its own; how many did what. ``measure``
    and ``count`` stand for the reads of the graph (the starter measure, what the questions run's sink reads)."""
    return await service.advance(
        engine,
        measure=measure or measure_starter,
        phase=kg_questions.phase,
        blobs=blobs,
        data_dir=data_dir,
        now=now,
        count=count,
    )


# The routes


REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}


@router.post(
    "/{project}/init",
    response_model=InitRun,
    status_code=201,
    responses={200: {"model": InitRun, "description": "a run is in flight already: that run"}, **REFUSALS},
)
async def start_init(request: Request, project: ProjectName, user: CurrentUser):
    """Ask for a kg_init run of the project now: 201 with the new run, or 200 with the run in flight."""
    from fastapi.responses import JSONResponse

    found, started = await service.start(request.app.state.engine, user, project)
    if started:
        return found
    return JSONResponse(found.model_dump(mode="json"), status_code=200)


@router.get("/{project}/init", response_model=InitRuns, responses={code: {"model": ErrorBody} for code in (403, 404)})
async def list_init(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=50, description="the newest runs listed")] = MAX_LISTED,
) -> InitRuns:
    """The project's kg_init runs, newest first, with the one in flight, its decisions and its report."""
    return await service.listing(request.app.state.engine, user, project, limit)


@router.post("/{project}/init/{run_id}/decision", response_model=DecisionOut, responses=REFUSALS)
async def decide_init(request: Request, project: ProjectName, run_id: int, body: DecisionBody, user: CurrentUser):
    """Record the Curator's decision of a kg_init run: validated, then applied (the strategy and settings, with the
    reasons and figures kept in the settings' history, a rebuild queued when needed)."""
    return DecisionOut(**await service.record_decision(request.app.state.engine, user, project, run_id, body.decision))


@admin_router.post("/kg-init", response_model=InitAll, status_code=201)
async def start_init_all(request: Request, user: CurrentUser) -> InitAll:
    """Queue a kg_init run for every project with a knowledge config (an admin's)."""
    return await service.start_all(request.app.state.engine, user)
