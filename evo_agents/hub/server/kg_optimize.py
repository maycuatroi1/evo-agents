"""The KG optimize loop of a project with a night shift (plan kg-retrieval-quality, criterion a7): each night inside
the charter's window, and whenever a writer asks (POST /v1/kg/{project}/optimize, ``evo-agents hub kg optimize``, the
button of the web's KG pages, the MCP tool kg_optimize), the hub measures the project's graph, decides what it can
decide itself, gives a review run to the worker on duty, and measures again after a new build. A run is a row of
kg_optimize_runs, which the job curator.collect moves on each minute (``advance``):

- ``measuring``: the holdout of the project's eval set asked of the latest build by words and by meaning, the tune
  split at each weight of the asked column (``evo_agents.hub.kg_optimize``), the open items of the review queue, the
  coverage of the build and what real sessions got from kg_search (kg_query_labels: click_rate, new labels). Then
  the gate of criterion a12 says whether search by meaning would be on (advice only: the loop no longer changes the
  mode, the Curator's kg_init run does), the weight of the asked
  column that ranks the tune split best is kept on the row (``kg_asked`` reads it at the next build), and an asked-for
  run queues its review run (``collect.queue_optimize_review``): the kg_health lens alone, at any hour, under the
  charter's review cost cap. A night's run is the night's review run, which gets the kg_health lens beside the
  rotation's (``collect.queue_review``).
- ``running``: until the review run ends. Its agent proposes fixes of the KG code, which the Builder makes on a branch
  and the Judge checks before the auto-merge.
- ``after``: the fixes of the review queue are applied as graph decisions through the path of POST
  /v1/kg/{project}/review/apply (origin kg-optimize, the review run), a build is queued, and once one newer than the
  first measure succeeded (or AFTER_WAIT passed) the graph is measured again. The loop undoes nothing: a holdout worse
  than before beyond the eval's noise is listed in ``regressions``, and ``handover`` (before and after, the weight and
  the mode in effect with the values to go back to, the decisions, the pull requests) is what the project's next
  kg_init run reads (``evo_agents.hub.server.kg_init``): the Curator decides keep or revert.
- At the end, ``done`` or ``failed`` (``reverted`` is of runs before kg_init), the owner of the night shift (and whoever asked) gets the report as
  a notification (notice kg_optimize), and the next morning brief has it.

The loop never turns on Jev's rerank (criterion a13). One run of a project is in flight at a time: a second call
returns the first.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub import kg_snapshots
from evo_agents.hub.jobs import JobQueue
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg.models.optimize import LOOP, MAX_DECISIONS, MAX_LISTED, OptimizeRun, OptimizeRuns
from evo_agents.hub.server.kg.service import optimize as service
from evo_agents.hub.server.kg.service.optimize import (  # noqa: F401 (collect, brief and the tests read them here)
    Measure,
    brief_facts,
    holds_review,
    link_agent_run,
    night_run,
    of_agent_run,
    with_lens,
)
from evo_agents.hub.server.security import MACHINE, CurrentUser, Principal

router = APIRouter(prefix="/v1/kg", tags=["kg"], responses={401: {"model": ErrorBody}})


# The fixes of the review queue


def _shim(engine: AsyncEngine, blobs, data_dir: Path):
    """What the routes of the review queue read of the request, ``app.state`` and an empty query (no ``branch``: the
    main graph), for the job."""
    state = SimpleNamespace(
        engine=engine,
        blobs=blobs,
        jobs=JobQueue.in_transaction(),
        kg_snapshots=kg_snapshots.SnapshotCoordinator(engine),
    )
    return SimpleNamespace(app=SimpleNamespace(state=state), query_params={})


def _owner(ctx) -> Principal:
    return Principal(user_id=ctx.owner_id, login=ctx.owner, admin=False, token_id=None, kind=MACHINE, token_hash="")  # type: ignore[arg-type]


async def _apply(engine: AsyncEngine, ctx, blobs, data_dir: Path, body: dict) -> None:
    """Apply the fixes of the open review items as decisions through POST /v1/kg/{project}/review/apply, as the owner
    of the night shift; what was decided in ``body["decisions"]``."""
    from evo_agents.hub.server.kg_review import ApplyBody, apply_review

    if ctx.owner_id is None:
        body["decisions"] = {"numbers": [], "why": "the project has no night shift owner to decide as"}
        return
    request = _shim(engine, blobs, data_dir)
    asked = ApplyBody(origin=LOOP, run_id=ctx.agent_run_id, max_decisions=MAX_DECISIONS)
    try:
        found = await apply_review(request, ctx.project, asked, _owner(ctx))
    except HTTPException as exc:
        body["decisions"] = {"numbers": [], "why": f"not applied: {exc.detail}"}
        return
    numbers = [d.number for item in found.items for d in item.decisions if d.number is not None]
    body["decisions"] = {"numbers": numbers, "rev": found.rev, "left": found.left, "why": "the review queue's fixes"}


async def advance(
    engine: AsyncEngine, *, blobs=None, data_dir=None, now: datetime | None = None, measure: Measure | None = None
) -> dict:
    """Move each KG optimize run in flight one phase on (see the module), each on its own; how many did what."""
    return await service.advance(engine, apply=_apply, blobs=blobs, data_dir=data_dir, now=now, measure=measure)


# The routes


START_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409)}


@router.post(
    "/{project}/optimize",
    response_model=OptimizeRun,
    status_code=201,
    responses={200: {"model": OptimizeRun, "description": "a run is in flight already: that run"}, **START_REFUSALS},
)
async def start_optimize(request: Request, project: ProjectName, user: CurrentUser):
    """Start a KG optimize run of the project now, at any hour, under the charter's review cost cap: 201 with the
    new run, or 200 with the run in flight, as one runs at a time."""
    from fastapi.responses import JSONResponse

    found, started = await service.start_run(request.app.state.engine, user, project)
    if started:
        return found
    return JSONResponse(found.model_dump(mode="json"), status_code=200)


@router.get(
    "/{project}/optimize",
    response_model=OptimizeRuns,
    responses={code: {"model": ErrorBody} for code in (403, 404)},
)
async def list_optimize(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=50, description="the newest runs listed")] = MAX_LISTED,
) -> OptimizeRuns:
    """The project's KG optimize runs, newest first, with the one in flight and each run's report."""
    return await service.list_runs(request.app.state.engine, user, project, limit)


async def mcp_answer(request: Request, project: str, user, arguments: dict, *, sink: str) -> tuple[str, dict]:
    """kg_optimize: start a KG optimize run (``action`` start, the default) or read the runs (status)."""
    del sink  # the answer shows figures and decisions, no graph data
    action = arguments.get("action") or "start"
    if action not in ("start", "status"):
        raise HTTPException(422, "action is start or status")
    engine = request.app.state.engine
    if action == "status":
        found = await service.list_runs(engine, user, project, 5)
        text = (
            f"KG optimize run #{found.active.id} of project {project} is {found.active.state}"
            if found.active
            else f"no KG optimize run of project {project} is in flight"
        )
        return text, found.model_dump(mode="json")
    run, started = await service.start_run(engine, user, project)
    word = "started" if started else f"in flight already ({run.state})"
    text = f"KG optimize run #{run.id} of project {project} {word}: the hub measures it within a minute"
    return text, {"run": run.model_dump(mode="json"), "started": started}
